import os
import shutil
import unittest

from mock import Mock, PropertyMock, patch
from tempfile import gettempdir

from patroni.async_executor import CriticalTask
from patroni.dcs import Leader, Member
from patroni.postgresql import Postgresql
from patroni.postgresql.bootstrap import Bootstrap
from patroni.postgresql.cancellable import CancellableSubprocess

from test_postgresql import psycopg2_connect


@patch('subprocess.call', Mock(return_value=0))
@patch('psycopg2.connect', psycopg2_connect)
@patch('os.rename', Mock())
class TestBootstrap(unittest.TestCase):

    @patch('subprocess.call', Mock(return_value=0))
    @patch('os.rename', Mock())
    def setUp(self):
        self.leadermem = Member(0, 'leader', 28, {'conn_url': 'postgres://replicator:rep-pass@127.0.0.1:5435/postgres'})
        self.leader = Leader(-1, 28, self.leadermem)
        self.p = Postgresql({'name': 'postgresql0', 'scope': 'dummy', 'listen': '127.0.0.2, 127.0.0.3:5432',
                             'data_dir': 'data/test0', 'retry_timeout': 10,
                             'pgpass': os.path.join(gettempdir(), 'pgpass0'),
                             'authentication': {'superuser': {'username': 'foo', 'password': 'bar'},
                                                'replication': {'username': '', 'password': ''}},
                             'remove_data_directory_on_rewind_failure': True,
                             'pg_hba': ['host all all 0.0.0.0/0 md5'],
                             'parameters': {'wal_level': 'hot_standby'}})
        if not os.path.exists(self.p.data_dir):
            os.makedirs(self.p.data_dir)
        self.b = self.p.bootstrap

    def tearDown(self):
        shutil.rmtree('data')

    @patch('time.sleep', Mock())
    @patch.object(CancellableSubprocess, 'call')
    @patch.object(Postgresql, 'remove_data_directory', Mock(return_value=True))
    @patch.object(Postgresql, 'data_directory_empty', Mock(return_value=False))
    @patch.object(Bootstrap, '_post_restore', Mock(side_effect=OSError))
    def test_create_replica(self, mock_cancellable_subprocess_call):
        self.p.config['create_replica_methods'] = ['pgBackRest']
        self.p.config['pgBackRest'] = {'command': 'pgBackRest', 'keep_data': True, 'no_params': True}
        mock_cancellable_subprocess_call.return_value = 0
        self.assertEqual(self.b.create_replica(self.leader), 0)

        self.p.config['create_replica_methods'] = ['wale', 'basebackup']
        self.p.config['wale'] = {'command': 'foo'}
        self.assertEqual(self.b.create_replica(self.leader), 0)
        del self.p.config['wale']
        self.assertEqual(self.b.create_replica(self.leader), 0)

        self.p.config['create_replica_methods'] = ['basebackup']
        self.p.config['basebackup'] = [{'max_rate': '100M'}, 'no-sync']
        self.assertEqual(self.b.create_replica(self.leader), 0)

        self.p.config['basebackup'] = [{'max_rate': '100M', 'compress': '9'}]
        with patch('patroni.postgresql.bootstrap.logger.error', new_callable=Mock()) as mock_logger:
            self.b.create_replica(self.leader)
            mock_logger.assert_called_once()
            self.assertTrue("only one key-value is allowed and value should be a string" in mock_logger.call_args[0][0],
                            "not matching {0}".format(mock_logger.call_args[0][0]))

        self.p.config['basebackup'] = [42]
        with patch('patroni.postgresql.bootstrap.logger.error', new_callable=Mock()) as mock_logger:
            self.b.create_replica(self.leader)
            mock_logger.assert_called_once()
            self.assertTrue("value should be string value or a single key-value pair" in mock_logger.call_args[0][0],
                            "not matching {0}".format(mock_logger.call_args[0][0]))

        self.p.config['basebackup'] = {"foo": "bar"}
        self.assertEqual(self.b.create_replica(self.leader), 0)

        self.p.config['create_replica_methods'] = ['wale', 'basebackup']
        del self.p.config['basebackup']
        mock_cancellable_subprocess_call.return_value = 1
        self.assertEqual(self.b.create_replica(self.leader), 1)

        mock_cancellable_subprocess_call.side_effect = Exception('foo')
        self.assertEqual(self.b.create_replica(self.leader), 1)

        mock_cancellable_subprocess_call.side_effect = [1, 0]
        self.assertEqual(self.b.create_replica(self.leader), 0)

        mock_cancellable_subprocess_call.side_effect = [Exception(), 0]
        self.assertEqual(self.b.create_replica(self.leader), 0)

        self.p.cancellable.cancel()
        self.assertEqual(self.b.create_replica(self.leader), 1)

    @patch('time.sleep', Mock())
    @patch.object(CancellableSubprocess, 'call')
    @patch.object(Postgresql, 'remove_data_directory', Mock(return_value=True))
    @patch.object(Bootstrap, '_post_restore', Mock(side_effect=OSError))
    def test_create_replica_old_format(self, mock_cancellable_subprocess_call):
        """ The same test as before but with old 'create_replica_method'
            to test backward compatibility
        """
        self.p.config['create_replica_method'] = ['wale', 'basebackup']
        self.p.config['wale'] = {'command': 'foo'}
        mock_cancellable_subprocess_call.return_value = 0
        self.assertEqual(self.b.create_replica(self.leader), 0)
        del self.p.config['wale']
        self.assertEqual(self.b.create_replica(self.leader), 0)

        self.p.config['create_replica_method'] = ['basebackup']
        self.p.config['basebackup'] = [{'max_rate': '100M'}, 'no-sync']
        self.assertEqual(self.b.create_replica(self.leader), 0)

        self.p.config['create_replica_method'] = ['wale', 'basebackup']
        del self.p.config['basebackup']
        mock_cancellable_subprocess_call.return_value = 1
        self.assertEqual(self.b.create_replica(self.leader), 1)

    def test_basebackup(self):
        self.p.cancellable.cancel()
        self.b.basebackup(None, None, {'foo': 'bar'})

    def test__initdb(self):
        self.assertRaises(Exception, self.b.bootstrap, {'initdb': [{'pgdata': 'bar'}]})
        self.assertRaises(Exception, self.b.bootstrap, {'initdb': [{'foo': 'bar', 1: 2}]})
        self.assertRaises(Exception, self.b.bootstrap, {'initdb': [1]})
        self.assertRaises(Exception, self.b.bootstrap, {'initdb': 1})

    @patch.object(CancellableSubprocess, 'call', Mock())
    @patch.object(Postgresql, 'is_running', Mock(return_value=True))
    @patch.object(Postgresql, 'data_directory_empty', Mock(return_value=False))
    def test_bootstrap(self):
        with patch('subprocess.call', Mock(return_value=1)):
            self.assertFalse(self.b.bootstrap({}))

        config = {'users': {'replicator': {'password': 'rep-pass', 'options': ['replication']}}}

        with patch.object(Postgresql, 'is_running', Mock(return_value=False)):
            self.b.bootstrap(config)
        with open(os.path.join(self.p.data_dir, 'pg_hba.conf')) as f:
            lines = f.readlines()
            self.assertTrue('host all all 0.0.0.0/0 md5\n' in lines)

        self.p.config.pop('pg_hba')
        config.update({'post_init': '/bin/false',
                       'pg_hba': ['host replication replicator 127.0.0.1/32 md5',
                                  'hostssl all all 0.0.0.0/0 md5',
                                  'host all all 0.0.0.0/0 md5']})
        self.b.bootstrap(config)
        with open(os.path.join(self.p.data_dir, 'pg_hba.conf')) as f:
            lines = f.readlines()
            self.assertTrue('host replication replicator 127.0.0.1/32 md5\n' in lines)

    @patch.object(CancellableSubprocess, 'call')
    @patch.object(Postgresql, 'get_major_version', Mock(return_value=90600))
    def test_custom_bootstrap(self, mock_cancellable_subprocess_call):
        self.p.config.pop('pg_hba')
        config = {'method': 'foo', 'foo': {'command': 'bar'}}

        mock_cancellable_subprocess_call.return_value = 1
        self.assertFalse(self.b.bootstrap(config))

        mock_cancellable_subprocess_call.return_value = 0
        with patch('multiprocessing.Process', Mock(side_effect=Exception("42"))),\
                patch('os.path.isfile', Mock(return_value=True)),\
                patch('os.unlink', Mock()),\
                patch.object(Postgresql, 'save_configuration_files', Mock()),\
                patch.object(Postgresql, 'restore_configuration_files', Mock()),\
                patch.object(Postgresql, 'write_recovery_conf', Mock()):
            with self.assertRaises(Exception) as e:
                self.b.bootstrap(config)
            self.assertEqual(str(e.exception), '42')

            config['foo']['recovery_conf'] = {'foo': 'bar'}

            with self.assertRaises(Exception) as e:
                self.b.bootstrap(config)
            self.assertEqual(str(e.exception), '42')

        mock_cancellable_subprocess_call.side_effect = Exception
        self.assertFalse(self.b.bootstrap(config))

    @patch('time.sleep', Mock())
    @patch('os.unlink', Mock())
    @patch('shutil.copy', Mock())
    @patch('os.path.isfile', Mock(return_value=True))
    @patch.object(Bootstrap, 'call_post_bootstrap', Mock(return_value=True))
    @patch.object(Bootstrap, '_custom_bootstrap', Mock(return_value=True))
    @patch.object(Postgresql, 'start', Mock(return_value=True))
    @patch.object(Postgresql, 'get_major_version', Mock(return_value=110000))
    def test_post_bootstrap(self):
        config = {'method': 'foo', 'foo': {'command': 'bar'}}
        self.b.bootstrap(config)

        task = CriticalTask()
        with patch.object(Bootstrap, 'create_or_update_role', Mock(side_effect=Exception)):
            self.b.post_bootstrap({}, task)
            self.assertFalse(task.result)

        self.p.config.pop('pg_hba')
        self.b.post_bootstrap({}, task)
        self.assertTrue(task.result)

        self.b.bootstrap(config)
        with patch.object(Postgresql, 'pending_restart', PropertyMock(return_value=True)), \
                patch.object(Postgresql, 'restart', Mock()) as mock_restart:
            self.b.post_bootstrap({}, task)
            mock_restart.assert_called_once()

        self.b.bootstrap(config)
        self.p.set_state('stopped')
        self.p.reload_config({'authentication': {'superuser': {'username': 'p', 'password': 'p'},
                                                 'replication': {'username': 'r', 'password': 'r'},
                                                 'rewind': {'username': 'rw', 'password': 'rw'}},
                              'listen': '*', 'retry_timeout': 10, 'parameters': {'wal_level': '', 'hba_file': 'foo'}})
        with patch.object(Postgresql, 'restart', Mock()) as mock_restart:
            self.b.post_bootstrap({}, task)
            mock_restart.assert_called_once()

    @patch.object(CancellableSubprocess, 'call')
    def test_call_post_bootstrap(self, mock_cancellable_subprocess_call):
        mock_cancellable_subprocess_call.return_value = 1
        self.assertFalse(self.b.call_post_bootstrap({'post_init': '/bin/false'}))

        mock_cancellable_subprocess_call.return_value = 0
        self.p._superuser.pop('username')
        self.assertTrue(self.b.call_post_bootstrap({'post_init': '/bin/false'}))
        mock_cancellable_subprocess_call.assert_called()
        args, kwargs = mock_cancellable_subprocess_call.call_args
        self.assertTrue('PGPASSFILE' in kwargs['env'])
        self.assertEqual(args[0], ['/bin/false', 'postgres://127.0.0.2:5432/postgres'])

        mock_cancellable_subprocess_call.reset_mock()
        self.p._local_address.pop('host')
        self.assertTrue(self.b.call_post_bootstrap({'post_init': '/bin/false'}))
        mock_cancellable_subprocess_call.assert_called()
        self.assertEqual(mock_cancellable_subprocess_call.call_args[0][0], ['/bin/false', 'postgres://:5432/postgres'])

        mock_cancellable_subprocess_call.side_effect = OSError
        self.assertFalse(self.b.call_post_bootstrap({'post_init': '/bin/false'}))

    @patch('os.path.exists', Mock(return_value=True))
    @patch('os.unlink', Mock())
    @patch.object(Bootstrap, 'create_replica', Mock(return_value=0))
    def test_clone(self):
        self.b.clone(self.leader)
