# coding: utf-8
"""
testgres.py
        Postgres testing utility

This module was created under influence of Postgres TAP test feature
(PostgresNode.pm module). It can manage Postgres clusters: initialize,
edit configuration files, start/stop cluster, execute queries. The
typical flow may look like:

    with get_new_node('test') as node:
        node.init()
        node.start()
        result = node.psql('postgres', 'SELECT 1')
        print(result)
        node.stop()

    Or:

    with get_new_node('node1') as node1:
        node1.init().start()
        with node1.backup() as backup:
            with backup.spawn_primary('node2') as node2:
                res = node2.start().execute('postgres', 'select 2')
                print(res)

Copyright (c) 2016, Postgres Professional
"""

import os
import subprocess
import pwd
import shutil
import time
import six
import port_for

import threading
import logging
import select
import tempfile

from enum import Enum
from distutils.version import LooseVersion

# Try to use psycopg2 by default. If psycopg2 isn't available then use
# pg8000 which is slower but much more portable because uses only
# pure-Python code
try:
    import psycopg2 as pglib
except ImportError:
    try:
        import pg8000 as pglib
    except ImportError:
        raise ImportError("You must have psycopg2 or pg8000 modules installed")

# ports used by nodes
bound_ports = set()

# threads for loggers
util_threads = []

# chached initdb dir
cached_data_dir = None

# rows returned by PG_CONFIG
pg_config_data = {}

UTILS_LOG_FILE = "utils.log"
BACKUP_LOG_FILE = "backup.log"

DATA_DIR = "data"
LOGS_DIR = "logs"

DEFAULT_XLOG_METHOD = "fetch"


class TestgresConfig:
    """
    Global config (override default settings)
    """

    cache_pg_config = True
    cache_initdb = True


class TestgresException(Exception):
    """
    Base exception
    """

    pass


class ExecUtilException(TestgresException):
    """
    Stores exit code
    """

    def __init__(self, message, exit_code=0):
        super(ExecUtilException, self).__init__(message)
        self.exit_code = exit_code


class ClusterTestgresException(TestgresException):
    pass


class QueryException(TestgresException):
    pass


class TimeoutException(TestgresException):
    pass


class StartNodeException(TestgresException):
    pass


class InitNodeException(TestgresException):
    pass


class BackupException(TestgresException):
    pass


class CatchUpException(TestgresException):
    pass


class TestgresLogger(threading.Thread):
    """
    Helper class to implement reading from postgresql.log
    """

    def __init__(self, node_name, fd):
        assert callable(fd.readline)

        threading.Thread.__init__(self)

        self.fd = fd
        self.node_name = node_name
        self.stop_event = threading.Event()
        self.logger = logging.getLogger(node_name)
        self.logger.setLevel(logging.INFO)

    def run(self):
        while self.fd in select.select([self.fd], [], [], 0)[0]:
            line = self.fd.readline()
            if line:
                extra = {'node': self.node_name}
                self.logger.info(line.strip(), extra=extra)
            elif self.stopped():
                break
            else:
                time.sleep(0.1)

    def stop(self):
        self.stop_event.set()

    def stopped(self):
        return self.stop_event.isSet()


def log_watch(node_name, pg_logname):
    """
    Starts thread for node that redirects
    postgresql logs to python logging system
    """

    reader = TestgresLogger(node_name, open(pg_logname, 'r'))
    reader.start()

    global util_threads
    util_threads.append(reader)

    return reader


class NodeConnection(object):
    """
    Transaction wrapper returned by Node
    """

    def __init__(self,
                 parent_node,
                 dbname,
                 host="127.0.0.1",
                 username=None,
                 password=None):

        # Use default user if not specified
        username = username or default_username()

        self.parent_node = parent_node

        self.connection = pglib.connect(
            database=dbname,
            user=username,
            port=parent_node.port,
            host=host,
            password=password)

        self.cursor = self.connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def begin(self, isolation_level=0):
        # yapf: disable
        levels = [
            'read uncommitted',
            'read committed',
            'repeatable read',
            'serializable'
        ]

        # Check if level is int [0..3]
        if (isinstance(isolation_level, int) and
                isolation_level in range(0, 4)):

            # Replace index with isolation level type
            isolation_level = levels[isolation_level]

        # Or it might be a string
        elif (isinstance(isolation_level, six.text_type) and
              isolation_level.lower() in levels):

            # Nothing to do here
            pass

        # Something is wrong, emit exception
        else:
            raise QueryException(
                'Invalid isolation level "{}"'.format(isolation_level))

        self.cursor.execute(
            'SET TRANSACTION ISOLATION LEVEL {}'.format(isolation_level))

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def execute(self, query, *args):
        self.cursor.execute(query, args)

        try:
            res = self.cursor.fetchall()

            if isinstance(res, tuple):
                res = [tuple(t) for t in res]

            return res
        except Exception:
            return None

    def close(self):
        self.cursor.close()
        self.connection.close()


class NodeBackup(object):
    """
    Smart object responsible for backups
    """

    @property
    def log_file(self):
        return os.path.join(self.base_dir, BACKUP_LOG_FILE)

    def __init__(self,
                 node,
                 base_dir=None,
                 username=None,
                 xlog_method=DEFAULT_XLOG_METHOD):

        if not node.status():
            raise BackupException('Node must be running')

        # set default arguments
        username = username or default_username()
        base_dir = base_dir or tempfile.mkdtemp()

        # create directory if needed
        if base_dir and not os.path.exists(base_dir):
            os.makedirs(base_dir)

        self.original_node = node
        self.base_dir = base_dir
        self.available = True

        data_dir = os.path.join(self.base_dir, DATA_DIR)
        _params = [
            "-D{}".format(data_dir),
            "-p{}".format(node.port),
            "-U{}".format(username),
            "-X{}".format(xlog_method)
        ]
        _execute_utility("pg_basebackup", _params, self.log_file)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.cleanup()

    def _prepare_dir(self, destroy):
        """
        Provide a data directory for a copy of node.

        Args:
            destroy: should we convert this backup into a node?

        Returns:
            Path to data directory.
        """

        if not self.available:
            raise BackupException('Backup is exhausted')

        # Do we want to use this backup several times?
        available = not destroy

        if available:
            base_dir = tempfile.mkdtemp()

            data1 = os.path.join(self.base_dir, DATA_DIR)
            data2 = os.path.join(base_dir, DATA_DIR)

            try:
                # Copy backup to new data dir
                shutil.copytree(data1, data2)
            except Exception as e:
                raise BackupException(str(e))
        else:
            base_dir = self.base_dir

        # update value
        self.available = available

        return base_dir

    def spawn_primary(self, name, destroy=True, use_logging=False):
        """
        Create a primary node from a backup.

        Args:
            name: name for a new node (str).
            destroy: should we convert this backup into a node?
            use_logging: enable python logging.

        Returns:
            New instance of PostgresNode.
        """

        base_dir = self._prepare_dir(destroy)

        # build a new PostgresNode
        node = PostgresNode(name=name,
                            base_dir=base_dir,
                            master=self.original_node,
                            use_logging=use_logging)

        node.append_conf("postgresql.conf", "\n")
        node.append_conf("postgresql.conf", "port = {}".format(node.port))

        return node

    def spawn_replica(self, name, destroy=True, use_logging=False):
        """
        Create a replica of the original node from a backup.

        Args:
            name: name for a new node (str).
            destroy: should we convert this backup into a node?
            use_logging: enable python logging.

        Returns:
            New instance of PostgresNode.
        """

        node = self.spawn_primary(name, destroy, use_logging=use_logging)
        node._create_recovery_conf(self.original_node)

        return node

    def cleanup(self):
        if self.available:
            shutil.rmtree(self.base_dir, ignore_errors=True)
            self.available = False


class NodeStatus(Enum):
    """
    Status of a PostgresNode
    """

    Running, Stopped, Uninitialized = range(3)

    # for Python 3.x
    def __bool__(self):
        return self.value == NodeStatus.Running.value

    # for Python 2.x
    __nonzero__ = __bool__


class PostgresNode(object):
    def __init__(self,
                 name,
                 port=None,
                 base_dir=None,
                 use_logging=False,
                 master=None):
        global bound_ports

        self.master = master
        self.name = name
        self.host = '127.0.0.1'
        self.port = port or reserve_port()
        self.should_free_port = port is None
        self.base_dir = base_dir or tempfile.mkdtemp()
        self.should_rm_base_dir = base_dir is None
        self.use_logging = use_logging
        self.logger = None

        # create directory if needed
        if not os.path.exists(self.logs_dir):
            os.makedirs(self.logs_dir)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        global bound_ports

        # stop node if necessary
        self.cleanup()

        # free port if necessary
        self.free_port()

    @property
    def data_dir(self):
        return os.path.join(self.base_dir, DATA_DIR)

    @property
    def logs_dir(self):
        return os.path.join(self.base_dir, LOGS_DIR)

    @property
    def utils_logname(self):
        return os.path.join(self.logs_dir, UTILS_LOG_FILE)

    @property
    def connstr(self):
        return "port={}".format(self.port)

    def _create_recovery_conf(self, root_node):
        line = (
            "primary_conninfo='{} application_name={}'\n"
            "standby_mode=on\n"
        ).format(root_node.connstr, self.name)

        self.append_conf("recovery.conf", line)

    def init(self, allow_streaming=False, fsync=False, initdb_params=[]):
        """
        Perform initdb for this node.

        Args:
            allow_streaming: should this node add a hba entry for replication?
            fsync: should this node use fsync to keep data safe?
            initdb_params: parameters for initdb (list).

        Returns:
            This instance of PostgresNode.
        """

        postgres_conf = os.path.join(self.data_dir, "postgresql.conf")

        # We don't have to reinit it if data directory exists
        if os.path.isfile(postgres_conf):
            raise InitNodeException('Node is already intialized')

        # initialize this PostgreSQL node
        initdb_log = os.path.join(self.logs_dir, "initdb.log")
        _cached_initdb(self.data_dir, initdb_log, initdb_params)

        # initialize default config files
        self.default_conf(fsync=fsync)

        return self

    def default_conf(self, allow_streaming=True, fsync=False, log_statement='all'):
        """
        Apply default settings to this node.

        Args:
            allow_streaming: should this node add a hba entry for replication?
            fsync: should this node use fsync to keep data safe?
            log_statement: one of ('all', 'off', 'mod', 'ddl'), look at
                postgresql docs for more information

        Returns:
            This instance of PostgresNode.
        """

        postgres_conf = os.path.join(self.data_dir, "postgresql.conf")
        hba_conf = os.path.join(self.data_dir, "pg_hba.conf")

        # add parameters to hba file
        with open(hba_conf, "w") as conf:
            conf.write("# TYPE\tDATABASE\tUSER\tADDRESS\t\tMETHOD\n"
                       "local\tall\t\tall\t\t\ttrust\n"
                       "host\tall\t\tall\t127.0.0.1/32\ttrust\n"
                       "host\tall\t\tall\t::1/128\t\ttrust\n"
                       # replication
                       "local\treplication\tall\t\t\ttrust\n"
                       "host\treplication\tall\t127.0.0.1/32\ttrust\n"
                       "host\treplication\tall\t::1/128\t\ttrust\n")

        # add parameters to config file
        with open(postgres_conf, "w") as conf:
            if not fsync:
                conf.write("fsync = off\n")

            conf.write("log_statement = {}\n"
                       "listen_addresses = '{}'\n"
                       "port = {}\n".format(log_statement,
                                            self.host,
                                            self.port))

            if allow_streaming:
                cur_ver = LooseVersion(get_pg_version())
                min_ver = LooseVersion('9.6')

                # select a proper wal_level for PostgreSQL
                wal_level = "hot_standby" if cur_ver < min_ver else "replica"

                conf.write("max_wal_senders = 5\n"
                           "wal_keep_segments = 20\n"
                           "hot_standby = on\n"
                           "wal_level = {}\n".format(wal_level))

        return self

    def append_conf(self, filename, string):
        """
        Append line to a config file (i.e. postgresql.conf).

        Args:
            filename: name of the config file.
            string: string to be appended to config.

        Returns:
            This instance of PostgresNode.
        """

        config_name = os.path.join(self.data_dir, filename)
        with open(config_name, "a") as conf:
            conf.write(''.join([string, '\n']))

        return self

    def status(self):
        """
        Check this node's status.

        Returns:
            An instance of NodeStatus.
        """

        try:
            _params = ["status", "-D", self.data_dir]
            _execute_utility("pg_ctl", _params, self.utils_logname)
            return NodeStatus.Running

        except ExecUtilException as e:
            # Node is not running
            if e.exit_code == 3:
                return NodeStatus.Stopped

            # Node has no file dir
            elif e.exit_code == 4:
                return NodeStatus.Uninitialized

    def get_pid(self):
        """
        Return postmaster's pid if node is running, else 0.
        """

        if self.status():
            with open(os.path.join(self.data_dir, 'postmaster.pid')) as f:
                return int(f.readline())

        # for clarity
        return 0

    def get_control_data(self):
        """
        Return contents of pg_control file.
        """

        cur_ver = LooseVersion(get_pg_version())
        min_ver = LooseVersion('9.5')

        if cur_ver < min_ver:
            _params = [self.data_dir]
        else:
            _params = ["-D", self.data_dir]

        lines = _execute_utility("pg_controldata", _params, self.utils_logname)

        out_data = {}

        for line in lines:
            key, value = line.partition(':')[::2]
            out_data[key.strip()] = value.strip()

        return out_data

    def start(self, params=[]):
        """
        Start this node using pg_ctl.

        Args:
            params: additional arguments for _execute_utility().

        Returns:
            This instance of PostgresNode.
        """

        # choose log_filename
        if self.use_logging:
            tmpfile = tempfile.NamedTemporaryFile('w', dir=self.logs_dir, delete=False)
            log_filename = tmpfile.name

            self.logger = log_watch(self.name, log_filename)
        else:
            log_filename = os.path.join(self.logs_dir, "postgresql.log")

        # choose conf_filename
        conf_filename = os.path.join(self.data_dir, "postgresql.conf")

        # choose hba_filename
        hba_filename = os.path.join(self.data_dir, "pg_hba.conf")

        # choose recovery_filename
        recovery_filename = os.path.join(self.data_dir, "recovery.conf")

        _params = [
            "start",
            "-D{}".format(self.data_dir),
            "-l{}".format(log_filename),
            "-w"
        ] + params

        try:
            _execute_utility("pg_ctl", _params, self.utils_logname)

        except ExecUtilException as e:
            def print_node_file(node_file):
                if os.path.exists(node_file):
                    try:
                        with open(node_file, 'r') as f:
                            return f.read()
                    except:
                        pass
                return "### file not found ###\n"

            error_text = (
                "Cannot start node\n"
                "{}\n"  # pg_ctl log
                "{}:\n----\n{}\n"  # postgresql.log
                "{}:\n----\n{}\n"  # postgresql.conf
                "{}:\n----\n{}\n"  # pg_hba.conf
                "{}:\n----\n{}\n"  # recovery.conf
            ).format(str(e),
                     log_filename, print_node_file(log_filename),
                     conf_filename, print_node_file(conf_filename),
                     hba_filename, print_node_file(hba_filename),
                     recovery_filename, print_node_file(recovery_filename))

            raise StartNodeException(error_text)

        return self

    def stop(self, params=[]):
        """
        Stop this node using pg_ctl.

        Args:
            params: additional arguments for _execute_utility().

        Returns:
            This instance of PostgresNode.
        """

        _params = ["stop", "-D", self.data_dir, "-w"] + params
        _execute_utility("pg_ctl", _params, self.utils_logname)

        if self.logger:
            self.logger.stop()

        return self

    def restart(self, params=[]):
        """
        Restart this node using pg_ctl.

        Args:
            params: additional arguments for _execute_utility().

        Returns:
            This instance of PostgresNode.
        """

        _params = ["restart", "-D", self.data_dir, "-w"] + params
        _execute_utility("pg_ctl", _params,
                         self.utils_logname,
                         write_to_pipe=False)

        return self

    def reload(self, params=[]):
        """
        Reload config files using pg_ctl.

        Returns:
            This instance of PostgresNode.
        """

        _params = ["reload", "-D", self.data_dir, "-w"] + params
        _execute_utility("pg_ctl", _params, self.utils_logname)

        return self

    def pg_ctl(self, params):
        """
        Invoke pg_ctl with params.

        Returns:
            Stdout + stderr of pg_ctl.
        """

        _params = params + ["-D", self.data_dir, "-w"]
        return _execute_utility("pg_ctl", _params, self.utils_logname)

    def free_port(self):
        """
        Reclaim port owned by this node.
        """

        if self.should_free_port:
            release_port(self.port)

    def cleanup(self, max_attempts=3):
        """
        Stop node if needed and remove its data directory.

        Returns:
            This instance of PostgresNode.
        """

        attempts = 0

        # try stopping server
        while attempts < max_attempts:
            try:
                self.stop()
                break  # OK
            except ExecUtilException as e:
                pass   # one more time
            except Exception as e:
                break  # screw this

            attempts += 1

        # remove data directory if necessary
        if self.should_rm_base_dir:
            shutil.rmtree(self.data_dir, ignore_errors=True)

        return self

    def psql(self, dbname, query=None, filename=None, username=None):
        """
        Execute a query using psql.

        Args:
            dbname: database name to connect to (str).
            query: query to be executed (str).
            filename: file with a query (str).
            username: database user name (str).

        Returns:
            A tuple of (code, stdout, stderr).
        """

        psql = get_bin_path("psql")
        psql_params = [
            psql,
            "-XAtq",
            "-h{}".format(self.host),
            "-p{}".format(self.port),
            dbname
        ]

        if query:
            psql_params.extend(("-c", query))
        elif filename:
            psql_params.extend(("-f", filename))
        else:
            raise QueryException('Query or filename must be provided')

        # Specify user if needed
        if username:
            psql_params.extend(("-U", username))

        # start psql process
        process = subprocess.Popen(psql_params,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)

        # wait untill it finishes and get stdout and stderr
        out, err = process.communicate()
        return process.returncode, out, err

    def safe_psql(self, dbname, query, username=None):
        """
        Execute a query using psql.

        Args:
            dbname: database name to connect to (str).
            query: query to be executed (str).
            username: database user name (str).

        Returns:
            psql's output as str.
        """

        ret, out, err = self.psql(dbname, query, username=username)
        if ret:
            raise QueryException(six.text_type(err))
        return out

    def dump(self, dbname, filename=None):
        """
        Dump database using pg_dump.

        Args:
            dbname: database name to connect to (str).
            filename: output file (str).

        Returns:
            Path to file containing dump.
        """

        f, filename = filename or tempfile.mkstemp()
        os.close(f)

        _params = [
            "-p{}".format(self.port),
            "-f{}".format(filename),
            dbname
        ]

        _execute_utility("pg_dump", _params, self.utils_logname)

        return filename

    def restore(self, dbname, filename, username=None):
        """
        Restore database from pg_dump's file.

        Args:
            dbname: database name to connect to (str).
            filename: database dump taken by pg_dump (str).
        """

        self.psql(dbname=dbname, filename=filename, username=username)

    def poll_query_until(self,
                         dbname,
                         query,
                         username=None,
                         max_attempts=60,
                         sleep_time=1,
                         expected=True,
                         raise_programming_error=True,
                         raise_internal_error=True):
        """
        Run a query once a second until it returs 'expected'.

        Args:
            dbname: database name to connect to (str).
            query: query to be executed (str).
            username: database user name (str).
            max_attempts: how many times should we try?
            sleep_time: how long should we sleep after a failure?
            expected: what should be returned to break the cycle?
            raise_programming_error: mute ProgrammingError?
            raise_internal_error: mute InternalError?
        """

        attempts = 0
        while attempts < max_attempts:
            try:
                res = self.execute(dbname=dbname,
                                   query=query,
                                   username=username,
                                   commit=True)

                if expected is None and res is None:
                    return  # done

                if res is None:
                    raise QueryException('Query returned None')

                if len(res) == 0:
                    raise QueryException('Query returned 0 rows')

                if len(res[0]) == 0:
                    raise QueryException('Query returned 0 columns')

                if res[0][0]:
                    return  # done

            except pglib.ProgrammingError as e:
                if raise_programming_error:
                    raise e

            except pglib.InternalError as e:
                if raise_internal_error:
                    raise e

            time.sleep(sleep_time)
            attempts += 1

        raise TimeoutException('Query timeout')

    def execute(self, dbname, query, username=None, commit=False):
        """
        Execute a query and return all rows as list.

        Args:
            dbname: database name to connect to (str).
            query: query to be executed (str).
            username: database user name (str).
            commit: should we commit this query?

        Returns:
            A list of tuples representing rows.
        """

        with self.connect(dbname, username) as node_con:
            res = node_con.execute(query)
            if commit:
                node_con.commit()
            return res

    def backup(self, username=None, xlog_method=DEFAULT_XLOG_METHOD):
        """
        Perform pg_basebackup.

        Args:
            username: database user name (str).
            xlog_method: a method for collecting the logs ('fetch' | 'stream').

        Returns:
            A smart object of type NodeBackup.
        """

        return NodeBackup(node=self,
                          username=username,
                          xlog_method=xlog_method)

    def replicate(self, name, username=None,
                  xlog_method=DEFAULT_XLOG_METHOD,
                  use_logging=False):
        """
        Create a replica of this node.

        Args:
            name: replica's name (str).
            username: database user name (str).
            xlog_method: a method for collecting the logs ('fetch' | 'stream').
            use_logging: enable python logging.
        """

        backup = self.backup(username=username, xlog_method=xlog_method)
        return backup.spawn_replica(name, use_logging=use_logging)

    def catchup(self):
        """
        Wait until async replica catches up with its master.
        """

        master = self.master

        cur_ver = LooseVersion(get_pg_version())
        min_ver = LooseVersion('10')

        if cur_ver >= min_ver:
            poll_lsn = "select pg_current_wal_lsn()::text"
            wait_lsn = "select pg_last_wal_replay_lsn() >= '{}'::pg_lsn"
        else:
            poll_lsn = "select pg_current_xlog_location()::text"
            wait_lsn = "select pg_last_xlog_replay_location() >= '{}'::pg_lsn"

        if not master:
            raise CatchUpException("Master node is not specified")

        try:
            lsn = master.execute('postgres', poll_lsn)[0][0]
            self.poll_query_until('postgres', wait_lsn.format(lsn))
        except Exception as e:
            raise CatchUpException(str(e))

    def pgbench_init(self, dbname='postgres', scale=1, options=[]):
        """
        Prepare database for pgbench (create tables etc).

        Args:
            dbname: database name to connect to (str).
            scale: report this scale factor in output (int).
            options: additional options for pgbench (list).

        Returns:
            This instance of PostgresNode.
        """

        _params = [
            "-i",
            "-s{}".format(scale),
            "-p{}".format(self.port)
        ] + options + [dbname]

        _execute_utility("pgbench", _params, self.utils_logname)

        return self

    def pgbench(self, dbname='postgres', stdout=None, stderr=None, options=[]):
        """
        Spawn a pgbench process.

        Args:
            dbname: database name to connect to (str).
            stdout: stdout file to be used by Popen.
            stderr: stderr file to be used by Popen.
            options: additional options for pgbench (list).

        Returns:
            Process created by subprocess.Popen.
        """

        pgbench = get_bin_path("pgbench")
        params = [pgbench, "-p", "%i" % self.port] + options + [dbname]
        proc = subprocess.Popen(params, stdout=stdout, stderr=stderr)

        return proc

    def connect(self, dbname='postgres', username=None):
        """
        Connect to a database.

        Args:
            dbname: database name to connect to (str).
            username: database user name (str).

        Returns:
            An instance of NodeConnection.
        """

        return NodeConnection(parent_node=self,
                              dbname=dbname,
                              username=username)


def _cached_initdb(data_dir, initdb_logfile, initdb_params=[]):
    """
    Perform initdb or use cached node files.
    """

    def call_initdb(_data_dir):
        try:
            _params = [_data_dir, "-N"] + initdb_params
            _execute_utility("initdb", _params, initdb_logfile)
        except Exception as e:
            raise InitNodeException(str(e))

    # Call initdb if we have custom params
    if initdb_params or not TestgresConfig.cache_initdb:
        call_initdb(data_dir)
    # Else we can use cached dir
    else:
        global cached_data_dir

        # Initialize cached initdb
        if cached_data_dir is None:
            cached_data_dir = tempfile.mkdtemp()
            call_initdb(cached_data_dir)

        try:
            # Copy cached initdb to current data dir
            shutil.copytree(cached_data_dir, data_dir)
        except Exception as e:
            raise InitNodeException(str(e))


def _execute_utility(util, args, logfile, write_to_pipe=True):
    """
    Execute utility (pg_ctl, pg_dump etc) using get_bin_path().

    Args:
        util: utility to be executed (str).
        args: arguments for utility (list).
        logfile: stores stdout and stderr (str).

    Returns:
        stdout of executed utility.
    """

    with open(logfile, "a") as file_out, \
            open(os.devnull, "w") as devnull:  # hack for 2.7

        # choose file according to options
        stdout_file = subprocess.PIPE if write_to_pipe else devnull

        # run utility
        process = subprocess.Popen([get_bin_path(util)] + args,
                                   stdout=stdout_file,
                                   stderr=subprocess.STDOUT)

        # get result
        out, _ = process.communicate()
        out = '' if not out else out.decode('utf-8')

        # write new log entry
        file_out.write(''.join(map(lambda x: str(x) + ' ', [util] + args)))
        file_out.write('\n')
        file_out.write(out)

        if process.returncode:
            error_text = (
                "{} failed\n"
                "log:\n----\n{}\n"
            ).format(util, out)

            raise ExecUtilException(error_text, process.returncode)

        return out


def default_username():
    """
    Return current user.
    """

    return pwd.getpwuid(os.getuid())[0]


def get_bin_path(filename):
    """
    Return full path to an executable using PG_BIN or PG_CONFIG.
    """

    pg_bin_path = os.environ.get("PG_BIN")

    if pg_bin_path:
        return os.path.join(pg_bin_path, filename)

    pg_config = get_pg_config()

    if pg_config and "BINDIR" in pg_config:
        return os.path.join(pg_config["BINDIR"], filename)

    return filename


def get_pg_version():
    """
    Return PostgreSQL version using PG_BIN or PG_CONFIG.
    """

    pg_bin_path = os.environ.get("PG_BIN")

    if pg_bin_path:
        _params = ['--version']
        raw_ver = _execute_utility('psql', _params, os.devnull)
    else:
        raw_ver = get_pg_config()["VERSION"]

    # Cook version of PostgreSQL
    version = raw_ver.strip().split(" ")[-1] \
                     .partition('devel')[0] \
                     .partition('beta')[0] \
                     .partition('rc')[0]

    return version


def reserve_port():
    """
    Generate a new port and add it to 'bound_ports'.
    """

    port = port_for.select_random(exclude_ports=bound_ports)
    bound_ports.add(port)

    return port


def release_port(port):
    """
    Free port provided by reserve_port().
    """

    bound_ports.remove(port)


def get_pg_config():
    """
    Return output of pg_config.
    """

    global pg_config_data

    if TestgresConfig.cache_pg_config and pg_config_data:
        return pg_config_data

    data = {}
    pg_config_cmd = os.environ.get("PG_CONFIG") or "pg_config"
    out = six.StringIO(subprocess.check_output([pg_config_cmd],
                                               universal_newlines=True))
    for line in out:
        if line and "=" in line:
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()

    if TestgresConfig.cache_pg_config:
        pg_config_data.clear()
        pg_config_data.update(data)

    return data


def get_new_node(name, base_dir=None, use_logging=False):
    """
    Create a new node (select port automatically).

    Args:
        name: node's name (str).
        base_dir: path to node's data directory (str).
        use_logging: should we use custom logger?

    Returns:
        An instance of PostgresNode.
    """

    return PostgresNode(name=name, base_dir=base_dir, use_logging=use_logging)


def configure_testgres(**options):
    """
    Configure testgres.
    Look at TestgresConfig to check what can be changed.
    """

    for key, option in options.items():
        setattr(TestgresConfig, key, option)
