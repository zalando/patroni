"""patroni --generate-config machinery."""
import abc
import copy
import os
import psutil
import socket
import sys
import yaml

from getpass import getuser, getpass
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple, TYPE_CHECKING, Union
if TYPE_CHECKING:  # pragma: no cover
    from psycopg import Cursor
    from psycopg2 import cursor

from . import psycopg
from .config import Config
from .exceptions import PatroniException
from .postgresql.config import parse_dsn
from .postgresql.config import ConfigHandler
from .postgresql.misc import postgres_major_version_to_int
from .utils import get_major_version, patch_config, read_stripped


# Mapping between the libpq connection parameters and the environment variables.
# This dict should be kept in sync with `patroni.utils._AUTH_ALLOWED_PARAMETERS`
# (we use "username" in the Patroni config for some reason, other parameter names are the same).
_AUTH_ALLOWED_PARAMETERS_MAPPING = {
    'user': 'PGUSER',
    'password': 'PGPASSWORD',
    'sslmode': 'PGSSLMODE',
    'sslcert': 'PGSSLCERT',
    'sslkey': 'PGSSLKEY',
    'sslpassword': '',
    'sslrootcert': 'PGSSLROOTCERT',
    'sslcrl': 'PGSSLCRL',
    'sslcrldir': 'PGSSLCRLDIR',
    'gssencmode': 'PGGSSENCMODE',
    'channel_binding': 'PGCHANNELBINDING'
}
_NO_VALUE_MSG = '#FIXME'


def get_address() -> Tuple[str, str]:
    """Try to get hostname and the ip address for it returned by :func:`~socket.gethostname`.

    .. note::
        Can also return local ip.

    :returns: tuple consisting of the hostname returned by `~socket.gethostname`
        and the first element in the sorted list of the addresses returned by :func:`~socket.getaddrinfo`.
        Sorting guarantees it will prefer IPv4.

    :raises:
        :class:`PatroniException`: if :exc:`OSError` occured
    """
    hostname = None
    try:
        hostname = socket.gethostname()
        return hostname, sorted(socket.getaddrinfo(hostname, 0, socket.AF_UNSPEC, socket.SOCK_STREAM, 0),
                                key=lambda x: x[0])[0][4][0]
    except OSError as e:
        raise PatroniException(f'Failed to define ip address: {e}')


class AbstractConfigGenerator(abc.ABC):
    """Object representing the generated Patroni config."""

    _HOSTNAME, _IP = get_address()
    _TEMPLATE_CONFIG: Dict[str, Any] = {
        'scope': _NO_VALUE_MSG,
        'name': _HOSTNAME,
        'postgresql': {
            'data_dir': _NO_VALUE_MSG,
            'connect_address': _NO_VALUE_MSG + ':5432',
            'listen': _NO_VALUE_MSG + ':5432',
            'bin_dir': '',
            'authentication': {
                'superuser': {
                    'username': 'postgres',
                    'password': _NO_VALUE_MSG
                },
                'replication': {
                    'username': 'replicator',
                    'password': _NO_VALUE_MSG
                }
            }
        },
        'restapi': {
            'connect_address': _IP + ':8008',
            'listen': _IP + ':8008'
        }
    }

    def __init__(self, file: Optional[str]) -> None:
        """Set up the output file (if passed), helper vars and the minimal config structure.

        :param file: full path to the output file to be used
        """
        self.output_file = file

        self.pg_major = 0

        self.config: Dict[str, Any] = Config('', None).local_configuration  # Get values from env
        dynamic_config = Config.get_default_config()
        dynamic_config['postgresql']['parameters'] = dict(dynamic_config['postgresql']['parameters'])
        self.config.setdefault('bootstrap', {})['dcs'] = dynamic_config
        self.config.setdefault('postgresql', {})

    def _get_int_major_version(self) -> int:
        """Get major PostgreSQL version from the binary as an integer.

        :returns: an integer PostgreSQL major version representation gathered from the PostgreSQL binary.
            See :func:`~patroni.postgresql.misc.postgres_major_version_to_int` and
            :func:`~patroni.utils.get_major_version`.
        """
        postgres_bin = ((self.config.get('postgresql') or {}).get('bin_name') or {}).get('postgres', 'postgres')
        return postgres_major_version_to_int(get_major_version(self.config['postgresql'].get('bin_dir') or None,
                                                               postgres_bin))

    @abc.abstractmethod
    def generate(self) -> None:
        """Generate config and store in `self.config`."""

    def merge_with_template(self) -> None:
        """Merge current `self.config` with the template and update `self.config`."""
        temp_config = copy.deepcopy(self._TEMPLATE_CONFIG)
        patch_config(temp_config, self.config)
        self.config = temp_config

    def write_config(self) -> None:
        """Write current `self.config` to the output file if provided, to stdout otherwise."""
        if self.output_file:
            dir_path = os.path.dirname(self.output_file)
            if dir_path and not os.path.isdir(dir_path):
                os.makedirs(dir_path)
            with open(self.output_file, 'w', encoding='UTF-8') as output_file:
                yaml.safe_dump(self.config, output_file, default_flow_style=False, allow_unicode=True)
        else:
            yaml.safe_dump(self.config, sys.stdout, default_flow_style=False, allow_unicode=True)


class SampleConfigGenerator(AbstractConfigGenerator):
    """Object representing the generated sample Patroni config.

    Sane defults are used based on the gathered PG version.
    """

    def __init__(self, file: Optional[str] = None) -> None:
        """Additionally set the PG major version from the binary and run config generation.

        :param file: full path to the output file to be used
        """
        super().__init__(file)

        self.pg_major = self._get_int_major_version()
        self.generate()

    @property
    def get_auth_method(self) -> str:
        """Return the preferred authentication method for a specific PG version if provided or the default 'md5'.

        :returns: :class:`str` value for the preferred authentication method
        """
        return 'scram-sha-256' if self.pg_major and self.pg_major >= 100000 else 'md5'

    def generate(self) -> None:
        """Generate sample config using some sane defaults and update `self.config`."""
        self.config['postgresql']['parameters'] = {'password_encryption': self.get_auth_method}
        username = self.config["postgresql"]["authentication"]["replication"]["username"]
        self.config['postgresql']['pg_hba'] = [
            f'host all all all {self.get_auth_method}',
            f'host replication {username} all {self.get_auth_method}'
        ]

        # add version-specific configuration
        wal_keep_param = 'wal_keep_segments' if self.pg_major < 130000 else 'wal_keep_size'
        self.config['bootstrap']['dcs']['postgresql']['parameters'][wal_keep_param] =\
            ConfigHandler.CMDLINE_OPTIONS[wal_keep_param][0]

        self.config['bootstrap']['dcs']['postgresql']['use_pg_rewind'] = True
        if self.pg_major >= 110000:
            self.config['postgresql']['authentication'].setdefault(
                'rewind', {'username': 'rewind_user'}).setdefault('password', _NO_VALUE_MSG)

        del self.config['bootstrap']['dcs']['standby_cluster']

        self.merge_with_template()


class RunningClusterConfigGenerator(AbstractConfigGenerator):
    """Object representing the Patroni config generated using information gathered from a running instance."""

    def __init__(self, file: Optional[str] = None, dsn: Optional[str] = None) -> None:
        """Additionally store the passed dsn (if any) in both original and parsed version and run config generation.

        :param file: full path to the output file to be used
        :param dsn: DSN string for the local instance to get GUC values from

        :raises:
            :class:`PatroniException`: if DSN parsing failed
        """
        super().__init__(file)

        self.dsn = dsn
        self.parsed_dsn = {}
        if self.dsn:
            self.parsed_dsn = parse_dsn(self.dsn) or {}
            if not self.parsed_dsn:
                raise PatroniException('Failed to parse DSN string')

        self.generate()

    @property
    def _get_hba_conn_types(self) -> Tuple[str, ...]:
        """Return the connection types allowed. If pg_major is defined, adds additional params for 16+.

        :returns: tuple of the connetcion methods allowed
        """
        allowed_types = ('local', 'host', 'hostssl', 'hostnossl', 'hostgssenc', 'hostnogssenc')
        if self.pg_major and self.pg_major >= 160000:
            allowed_types += ('include', 'include_if_exists', 'include_dir')
        return allowed_types

    @property
    def _required_pg_params(self) -> List[str]:
        """PG configuration prameters that have to be always present in the generated config.

        :returns:
        """
        return ['hba_file', 'ident_file', 'config_file', 'data_directory'] + \
            list(ConfigHandler.CMDLINE_OPTIONS.keys())

    def _get_bin_dir_from_running_instance(self) -> str:
        """Define the directory postgres binaries reside using postmaster's pid executable.

        :param data_dir: the PostgreSQL data directory to search for postmaster.pid file in.

        :returns: path to the PostgreSQL binaries directory

        :raises:
            :class:`PatroniException`: if:

                * pid could not be obtained from the `postmaster.pid` file; or
                * :exc:`OSError` occured during `postmaster.pid` file handling; or
                * the obrained postmaster pid doesn't exist.
        """
        postmaster_pid = None
        data_dir = self.config['postgresql']['data_dir']
        try:
            with open(f"{data_dir}/postmaster.pid", 'r') as pid_file:
                postmaster_pid = pid_file.readline()
                if not postmaster_pid:
                    raise PatroniException('Failed to obtain postmaster pid from postmaster.pid file')
                postmaster_pid = int(postmaster_pid.strip())
        except OSError as e:
            raise PatroniException(f'Error while reading postmaster.pid file: {e}')
        try:
            return os.path.dirname(psutil.Process(postmaster_pid).exe())
        except psutil.NoSuchProcess:
            raise PatroniException("Obtained postmaster pid doesn't exist.")

    @contextmanager
    def _get_connection_cursor(self) -> Iterator[Union['cursor', 'Cursor[Any]']]:
        """Get cursor for the PG connection established based on the stored information.

        :raises:
            :class:`PatroniException`: if :exc:`psycopg.Error` occured.
        """
        try:
            conn = psycopg.connect(dsn=self.dsn,
                                   password=self.config['postgresql']['authentication']['superuser']['password'])
            with conn.cursor() as cur:
                yield cur
            conn.close()
        except psycopg.Error as e:
            raise PatroniException(f'Failed to establish PostgreSQL connection: {e}')

    def _is_superuser(self, cur: Union['cursor', 'Cursor[Any]'], username: str) -> bool:
        """Check if the user has superuser privilege.

        :param cur: connection cursor to use for the check.
        :param username: username to check.
        """
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s AND rolsuper", (username,))
        return cur.rowcount == 1

    def _set_pg_params(self, cur: Union['cursor', 'Cursor[Any]']) -> None:
        """Extend `self.config` with the actual PG GUCs values.

        THe following GUC values are set:

            * Non-internal having configuration file, postmaster command line or environment variable
              as a source.
            * List of the always required parameters (see :func:`_required_pg_params`)

        :param cur: connection cursor to use.
        """
        cur.execute("SELECT name, current_setting(name) FROM pg_settings "
                    "WHERE context <> 'internal' "
                    "AND source IN ('configuration file', 'command line', 'environment variable') "
                    "AND category <> 'Write-Ahead Log / Recovery Target' "
                    "AND setting <> '(disabled)' "
                    "OR name = ANY(%s)", (self._required_pg_params,))

        helper_dict = dict.fromkeys(['port', 'listen_addresses'])
        # adjust values
        self.config['postgresql'].setdefault('parameters', {})
        for p, v in cur.fetchall():
            if p == 'data_directory':
                self.config['postgresql']['data_dir'] = v
            elif p == 'cluster_name' and v:
                self.config['scope'] = v
            elif p in ('archive_command', 'restore_command', 'archive_cleanup_command',
                       'recovery_end_command', 'ssl_passphrase_command',
                       'hba_file', 'ident_file', 'config_file'):
                # write commands to the local config due to security implications
                # write hba/ident/config_file to local config to ensure they are not removed later
                self.config['postgresql']['parameters'][p] = v
            elif p in helper_dict:
                helper_dict[p] = v
            else:
                self.config['bootstrap']['dcs']['postgresql']['parameters'][p] = v

        connect_port = self.parsed_dsn.get('port', os.getenv('PGPORT', helper_dict['port']))
        self.config['postgresql']['connect_address'] = f'{self._IP}:{connect_port}'
        self.config['postgresql']['listen'] = f'{helper_dict["listen_addresses"]}:{helper_dict["port"]}'

    def _set_su_params(self) -> None:
        """Extend `self.config` with the superuser auth information using the options used for connection."""
        su_params: Dict[str, str] = {}
        for conn_param, env_var in _AUTH_ALLOWED_PARAMETERS_MAPPING.items():
            val = self.parsed_dsn.get(conn_param, os.getenv(env_var))
            if val:
                su_params[conn_param] = val
        patroni_env_su_username = ((self.config.get('authentication') or {}).get('superuser') or {}).get('username')
        patroni_env_su_pwd = ((self.config.get('authentication') or {}).get('superuser') or {}).get('password')
        # because we use "username" in the config for some reason
        su_params['username'] = su_params.pop('user', patroni_env_su_username) or getuser()
        su_params['password'] = su_params.get('password', patroni_env_su_pwd) or \
            getpass('Please enter the user password:')
        self.config['postgresql']['authentication'] = {
            'superuser': su_params,
            'replication': {'username': _NO_VALUE_MSG, 'password': _NO_VALUE_MSG}
        }

    def _set_conf_files(self) -> None:
        """Extend `self.config` with the information from ``pg_hba.conf`` and ``pg_ident.conf`` files.

        .. note::
            This function only defines ``postgresql.pg_hba`` and ``postgresql.pg_ident`` when
            ``hba_file`` and ``ident_file`` are set to the defaults.

        :raises:
            :class:`PatroniException`: if :exc:`OSError` occured during the conf files handling.
        """
        default_hba_path = os.path.join(self.config['postgresql']['data_dir'], 'pg_hba.conf')
        if self.config['postgresql']['parameters']['hba_file'] == default_hba_path:
            try:
                self.config['postgresql']['pg_hba'] = list(
                    filter(lambda i: i and i.split()[0] in self._get_hba_conn_types, read_stripped(default_hba_path)))
            except OSError as e:
                raise PatroniException(f'Failed to read pg_hba.conf: {e}')

        default_ident_path = os.path.join(self.config['postgresql']['data_dir'], 'pg_ident.conf')
        if self.config['postgresql']['parameters']['ident_file'] == default_ident_path:
            try:
                self.config['postgresql']['pg_ident'] = [i for i in read_stripped(default_ident_path)
                                                         if i and not i.startswith('#')]
            except OSError as e:
                raise PatroniException(f'Failed to read pg_ident.conf: {e}')
            if not self.config['postgresql']['pg_ident']:
                del self.config['postgresql']['pg_ident']

    def _enrich_config_from_running_instance(self) -> None:
        """Extend `self.config` dictionary with the values gathered from a running instance.

        Retrieve the following information from the running PostgreSQL instance:

        * superuser auth parameters (see :func:`_set_su_params`)
        * some GUC values (see :func:`_set_pg_params`)
        * ``postgresql.connect_address``, postgresql.listen``
        * ``postgresql.pg_hba`` and ``postgresql.pg_ident`` (see :func:`_set_conf_files`)

        And redefine ``scope`` with the ``cluster_name`` GUC value if set.

        :raises:
            :class:`PatroniException`: if the provided user doesn't have superuser privilege.
        """
        self._set_su_params()

        with self._get_connection_cursor() as cur:
            self.pg_major = getattr(cur.connection, 'server_version', 0)

            if not self._is_superuser(cur, self.config['postgresql']['authentication']['superuser']['username']):
                raise PatroniException('The provided user does not have superuser privilege')

            self._set_pg_params(cur)

        self._set_conf_files()

    def generate(self) -> None:
        """Generate config using the info gathered from the specified running PG instance and update `self.config`."""
        self._enrich_config_from_running_instance()
        self.config['postgresql']['bin_dir'] = self._get_bin_dir_from_running_instance()
        del self.config['bootstrap']['dcs']['standby_cluster']

        self.merge_with_template()


def generate_config(file: str, sample: bool, dsn: Optional[str]) -> None:
    """Generate Patroni configuration file.

    Gather all the available non-internal GUC values having configuration file, postmaster command line or environment
    variable as a source and store them in the appropriate part of Patroni configuration (``postgresql.parameters`` or
    ``bootsrtap.dcs.postgresql.parameters``). Either the provided DSN (takes precedence) or PG ENV vars will be used
    for the connection. If password is not provided, it should be entered via prompt.

    The created configuration contains:
    * ``scope``: cluster_name GUC value or PATRONI_SCOPE ENV variable value if available
    * ``name``: PATRONI_NAME ENV variable value if set, otherewise hostname
    * ``bootsrtap.dcs``: section with all the parameters (incl. the majority of PG GUCs) set to their default values
      defined by Patroni and adjusted by the source instances's configuration values.
    * ``postgresql.parameters``: the source instance's archive_command, restore_command, archive_cleanup_command,
      recovery_end_command, ssl_passphrase_command, hba_file, ident_file, config_file GUC values
    * ``postgresql.bin_dir``: path to Postgres binaries gathered from the running instance or, if not available,
      the value of PATRONI_POSTGRESQL_BIN_DIR ENV variable. Otherwise, an empty string.
    * ``postgresql.datadir``: the value gathered from the corresponding PG GUC
    * ``postgresql.listen``: source instance's listen_addresses and port GUC values
    * ``postgresql.connect_address``: if possible, generated from the connection params
    * ``postgresql.authentication``:

        * superuser and replication users defined (if possible, usernames are set from the respective Patroni ENV vars,
          otherwise the default 'postgres' and 'replicator' values are used).
          If not a sample config, either DSN or PG ENV vars are used to define superuser authentication parameters.
        * rewind user is defined only for sample config, if PG version can be defined and PG version is 11+
          (if possible, username is set from the respective Patroni ENV var)

    * ``bootsrtap.dcs.postgresql.use_pg_rewind``
    * ``postgresql.pg_hba`` defaults or the lines gathered from the source instance's hba_file
    * ``postgresql.pg_ident`` the lines gathered from the source instance's ident_file

    .. note::
        In case :class:`PatroniException` is raised by any of the called methods, execution is terminated.

    :param file: Full path to the configuration file to be used. If not provided, result is sent to stdout.
    :param sample: Optional flag. If set, no source instance will be used - generate config with some sane defaults.
    :param dsn: Optional DSN string for the local instance to get GUC values from.
    """
    try:
        if sample:
            config_generator = SampleConfigGenerator(file)
        else:
            config_generator = RunningClusterConfigGenerator(file, dsn)

        config_generator.write_config()
    except PatroniException as e:
        sys.exit(str(e))