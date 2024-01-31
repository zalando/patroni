import logging
import mock
from mock import MagicMock, Mock, patch
import unittest
from urllib3.exceptions import MaxRetryError

from patroni.scripts.barman.cli import main
from patroni.scripts.barman.config_switch import (BarmanConfigSwitch, ExitCode as BarmanConfigSwitchExitCode,
                                                  _should_skip_switch, run_barman_config_switch)
from patroni.scripts.barman.recover import BarmanRecover, ExitCode as BarmanRecoverExitCode, run_barman_recover
from patroni.scripts.barman.utils import ApiNotOk, OperationStatus, PgBackupApi, RetriesExceeded, set_up_logging


API_URL = "http://localhost:7480"
BARMAN_SERVER = "my_server"
BARMAN_MODEL = "my_model"
BACKUP_ID = "backup_id"
SSH_COMMAND = "ssh postgres@localhost"
DATA_DIRECTORY = "/path/to/pgdata"
LOOP_WAIT = 10
RETRY_WAIT = 2
MAX_RETRIES = 5


# stuff from patroni.scripts.barman.utils

@patch("logging.basicConfig")
def test_set_up_logging(mock_log_config):
    log_file = "/path/to/some/file.log"
    set_up_logging(log_file)
    mock_log_config.assert_called_once_with(filename=log_file, level=logging.INFO,
                                            format="%(asctime)s %(levelname)s: %(message)s")


class TestPgBackupApi(unittest.TestCase):

    @patch.object(PgBackupApi, "_ensure_api_ok", Mock())
    @patch("patroni.scripts.barman.utils.PoolManager", MagicMock())
    def setUp(self):
        self.api = PgBackupApi(API_URL, None, None, RETRY_WAIT, MAX_RETRIES)
        # Reset the mock as the same instance is used across tests
        self.api._http.request.reset_mock()
        self.api._http.request.side_effect = None

    def test__build_full_url(self):
        self.assertEqual(self.api._build_full_url("/some/path"), f"{API_URL}/some/path")

    @patch("json.loads")
    def test__deserialize_response(self, mock_json_loads):
        mock_response = MagicMock()
        self.assertIsNotNone(self.api._deserialize_response(mock_response))
        mock_json_loads.assert_called_once_with(mock_response.data.decode("utf-8"))

    @patch("json.dumps")
    def test__serialize_request(self, mock_json_dumps):
        body = "some_body"
        ret = self.api._serialize_request(body)
        self.assertIsNotNone(ret)
        mock_json_dumps.assert_called_once_with(body)
        mock_json_dumps.return_value.encode.assert_called_once_with("utf-8")

    @patch.object(PgBackupApi, "_deserialize_response", Mock(return_value="test"))
    def test__get_request(self):
        mock_request = self.api._http.request

        # with no error
        self.assertEqual(self.api._get_request("/some/path"), "test")
        mock_request.assert_called_once_with("GET", f"{API_URL}/some/path")

        # with MaxRetryError
        http_error = MaxRetryError(self.api._http, f"{API_URL}/some/path")
        mock_request.side_effect = http_error

        with self.assertRaises(RetriesExceeded) as exc:
            self.assertIsNone(self.api._get_request("/some/path"))

        self.assertEqual(
            str(exc.exception),
            "Failed to perform a GET request to http://localhost:7480/some/path"
        )

    @patch.object(PgBackupApi, "_deserialize_response", Mock(return_value="test"))
    @patch.object(PgBackupApi, "_serialize_request")
    def test__post_request(self, mock_serialize):
        mock_request = self.api._http.request

        # with no error
        self.assertEqual(self.api._post_request("/some/path", "some body"), "test")
        mock_serialize.assert_called_once_with("some body")
        mock_request.assert_called_once_with("POST", f"{API_URL}/some/path", body=mock_serialize.return_value,
                                             headers={"Content-Type": "application/json"})

        # with HTTPError
        http_error = MaxRetryError(self.api._http, f"{API_URL}/some/path")
        mock_request.side_effect = http_error

        with self.assertRaises(RetriesExceeded) as exc:
            self.assertIsNone(self.api._post_request("/some/path", "some body"))

        self.assertEqual(
            str(exc.exception),
            f"Failed to perform a POST request to http://localhost:7480/some/path with {mock_serialize.return_value}"
        )

    @patch.object(PgBackupApi, "_get_request")
    def test__ensure_api_ok(self, mock_get_request):
        # API ok
        mock_get_request.return_value = "OK"
        self.assertIsNone(self.api._ensure_api_ok())

        # API not ok
        mock_get_request.return_value = "random"

        with self.assertRaises(ApiNotOk) as exc:
            self.assertIsNone(self.api._ensure_api_ok())

        self.assertEqual(
            str(exc.exception),
            "pg-backup-api is currently not up and running at http://localhost:7480: random",
        )

    @patch("patroni.scripts.barman.utils.OperationStatus")
    @patch("logging.warning")
    @patch("time.sleep")
    @patch.object(PgBackupApi, "_get_request")
    def test_get_operation_status(self, mock_get_request, mock_sleep, mock_logging, mock_op_status):
        # well formed response
        mock_get_request.return_value = {"status": "some status"}
        mock_op_status.__getitem__.return_value = "SOME_STATUS"
        self.assertEqual(self.api.get_operation_status(BARMAN_SERVER, "some_id"), "SOME_STATUS")
        mock_get_request.assert_called_once_with(f"servers/{BARMAN_SERVER}/operations/some_id")
        mock_sleep.assert_not_called()
        mock_logging.assert_not_called()
        mock_op_status.__getitem__.assert_called_once_with("some status")

        # malformed response
        mock_get_request.return_value = {"statuss": "some status"}

        with self.assertRaises(RetriesExceeded) as exc:
            self.api.get_operation_status(BARMAN_SERVER, "some_id")

        self.assertEqual(str(exc.exception),
                         "Maximum number of retries exceeded for method PgBackupApi.get_operation_status.")

        self.assertEqual(mock_sleep.call_count, self.api.max_retries)
        mock_sleep.assert_has_calls([mock.call(self.api.retry_wait)] * self.api.max_retries)

        self.assertEqual(mock_logging.call_count, self.api.max_retries)
        for i in range(mock_logging.call_count):
            call_args = mock_logging.call_args_list[i][0]
            self.assertEqual(len(call_args), 5)
            self.assertEqual(call_args[0], "Attempt %d of %d on method %s failed with %r.")
            self.assertEqual(call_args[1], i + 1)
            self.assertEqual(call_args[2], self.api.max_retries)
            self.assertEqual(call_args[3], "PgBackupApi.get_operation_status")
            self.assertIsInstance(call_args[4], KeyError)
            self.assertEqual(call_args[4].args, ('status',))

    @patch("logging.warning")
    @patch("time.sleep")
    @patch.object(PgBackupApi, "_post_request")
    def test_create_recovery_operation(self, mock_post_request, mock_sleep, mock_logging):
        # well formed response
        mock_post_request.return_value = {"operation_id": "some_id"}
        self.assertEqual(
            self.api.create_recovery_operation(BARMAN_SERVER, BACKUP_ID, SSH_COMMAND, DATA_DIRECTORY),
            "some_id",
        )
        mock_sleep.assert_not_called()
        mock_logging.assert_not_called()
        mock_post_request.assert_called_once_with(
            f"servers/{BARMAN_SERVER}/operations",
            {
                "type": "recovery",
                "backup_id": BACKUP_ID,
                "remote_ssh_command": SSH_COMMAND,
                "destination_directory": DATA_DIRECTORY,
            }
        )

        # malformed response
        mock_post_request.return_value = {"operation_idd": "some_id"}

        with self.assertRaises(RetriesExceeded) as exc:
            self.api.create_recovery_operation(BARMAN_SERVER, BACKUP_ID, SSH_COMMAND, DATA_DIRECTORY)

        self.assertEqual(str(exc.exception),
                         "Maximum number of retries exceeded for method PgBackupApi.create_recovery_operation.")

        self.assertEqual(mock_sleep.call_count, self.api.max_retries)

        mock_sleep.assert_has_calls([mock.call(self.api.retry_wait)] * self.api.max_retries)

        self.assertEqual(mock_logging.call_count, self.api.max_retries)
        for i in range(mock_logging.call_count):
            call_args = mock_logging.call_args_list[i][0]
            self.assertEqual(len(call_args), 5)
            self.assertEqual(call_args[0], "Attempt %d of %d on method %s failed with %r.")
            self.assertEqual(call_args[1], i + 1)
            self.assertEqual(call_args[2], self.api.max_retries)
            self.assertEqual(call_args[3], "PgBackupApi.create_recovery_operation")
            self.assertIsInstance(call_args[4], KeyError)
            self.assertEqual(call_args[4].args, ('operation_id',))

    @patch("logging.warning")
    @patch("time.sleep")
    @patch.object(PgBackupApi, "_post_request")
    def test_create_config_switch_operation(self, mock_post_request, mock_sleep, mock_logging):
        # well formed response -- sample 1
        mock_post_request.return_value = {"operation_id": "some_id"}
        self.assertEqual(
            self.api.create_config_switch_operation(BARMAN_SERVER, BARMAN_MODEL, None),
            "some_id",
        )
        mock_sleep.assert_not_called()
        mock_logging.assert_not_called()
        mock_post_request.assert_called_once_with(
            f"servers/{BARMAN_SERVER}/operations",
            {
                "type": "config_switch",
                "model_name": BARMAN_MODEL,
            }
        )

        # well formed response -- sample 2
        mock_post_request.reset_mock()

        self.assertEqual(
            self.api.create_config_switch_operation(BARMAN_SERVER, None, True),
            "some_id",
        )
        mock_sleep.assert_not_called()
        mock_logging.assert_not_called()
        mock_post_request.assert_called_once_with(
            f"servers/{BARMAN_SERVER}/operations",
            {
                "type": "config_switch",
                "reset": True,
            }
        )

        # malformed response
        mock_post_request.return_value = {"operation_idd": "some_id"}

        with self.assertRaises(RetriesExceeded) as exc:
            self.api.create_config_switch_operation(BARMAN_SERVER, BARMAN_MODEL, None)

        self.assertEqual(str(exc.exception),
                         "Maximum number of retries exceeded for method PgBackupApi.create_config_switch_operation.")

        self.assertEqual(mock_sleep.call_count, self.api.max_retries)

        mock_sleep.assert_has_calls([mock.call(self.api.retry_wait)] * self.api.max_retries)

        self.assertEqual(mock_logging.call_count, self.api.max_retries)
        for i in range(mock_logging.call_count):
            call_args = mock_logging.call_args_list[i][0]
            self.assertEqual(len(call_args), 5)
            self.assertEqual(call_args[0], "Attempt %d of %d on method %s failed with %r.")
            self.assertEqual(call_args[1], i + 1)
            self.assertEqual(call_args[2], self.api.max_retries)
            self.assertEqual(call_args[3], "PgBackupApi.create_config_switch_operation")
            self.assertIsInstance(call_args[4], KeyError)
            self.assertEqual(call_args[4].args, ('operation_id',))


# stuff from patroni.scripts.barman.recover


class TestBarmanRecover(unittest.TestCase):

    @patch("patroni.scripts.barman.recover.PgBackupApi", MagicMock())
    def setUp(self):
        self.br = BarmanRecover(BARMAN_SERVER, BACKUP_ID, SSH_COMMAND,
                                DATA_DIRECTORY, LOOP_WAIT, API_URL, None, None,
                                RETRY_WAIT, MAX_RETRIES)
        # Reset the mock as the same instance is used across tests
        self.br._api._http.request.reset_mock()
        self.br._api._http.request.side_effect = None

    @patch("time.sleep")
    @patch("logging.info")
    @patch("logging.error")
    def test_restore_backup(self, mock_log_error, mock_log_info, mock_sleep):
        mock_create_op = self.br._api.create_recovery_operation
        mock_get_status = self.br._api.get_operation_status

        # successful fast restore
        mock_create_op.return_value = "some_id"
        mock_get_status.return_value = OperationStatus.DONE

        self.assertTrue(self.br.restore_backup())

        mock_create_op.assert_called_once_with(BARMAN_SERVER, BACKUP_ID, SSH_COMMAND, DATA_DIRECTORY)
        mock_get_status.assert_called_once_with(BARMAN_SERVER, "some_id")
        mock_log_info.assert_called_once_with("Created the recovery operation with ID %s", "some_id")
        mock_log_error.assert_not_called()
        mock_sleep.assert_not_called()

        # successful slow restore
        mock_create_op.reset_mock()
        mock_get_status.reset_mock()
        mock_log_info.reset_mock()
        mock_get_status.side_effect = [OperationStatus.IN_PROGRESS] * 20 + [OperationStatus.DONE]

        self.assertTrue(self.br.restore_backup())

        mock_create_op.assert_called_once()

        self.assertEqual(mock_get_status.call_count, 21)
        mock_get_status.assert_has_calls([mock.call(BARMAN_SERVER, "some_id")] * 21)

        self.assertEqual(mock_log_info.call_count, 21)
        mock_log_info.assert_has_calls([mock.call("Created the recovery operation with ID %s", "some_id")]
                                       + [mock.call("Recovery operation %s is still in progress", "some_id")] * 20)

        mock_log_error.assert_not_called()

        self.assertEqual(mock_sleep.call_count, 20)
        mock_sleep.assert_has_calls([mock.call(LOOP_WAIT)] * 20)

        # failed fast restore
        mock_create_op.reset_mock()
        mock_get_status.reset_mock()
        mock_log_info.reset_mock()
        mock_sleep.reset_mock()
        mock_get_status.side_effect = None
        mock_get_status.return_value = OperationStatus.FAILED

        self.assertFalse(self.br.restore_backup())

        mock_create_op.assert_called_once()
        mock_get_status.assert_called_once_with(BARMAN_SERVER, "some_id")
        mock_log_info.assert_called_once_with("Created the recovery operation with ID %s", "some_id")
        mock_log_error.assert_not_called()
        mock_sleep.assert_not_called()

        # failed slow restore
        mock_create_op.reset_mock()
        mock_get_status.reset_mock()
        mock_log_info.reset_mock()
        mock_sleep.reset_mock()
        mock_get_status.side_effect = [OperationStatus.IN_PROGRESS] * 20 + [OperationStatus.FAILED]

        self.assertFalse(self.br.restore_backup())

        mock_create_op.assert_called_once()

        self.assertEqual(mock_get_status.call_count, 21)
        mock_get_status.assert_has_calls([mock.call(BARMAN_SERVER, "some_id")] * 21)

        self.assertEqual(mock_log_info.call_count, 21)
        mock_log_info.assert_has_calls([mock.call("Created the recovery operation with ID %s", "some_id")]
                                       + [mock.call("Recovery operation %s is still in progress", "some_id")] * 20)

        mock_log_error.assert_not_called()

        self.assertEqual(mock_sleep.call_count, 20)
        mock_sleep.assert_has_calls([mock.call(LOOP_WAIT)] * 20)

        # create retries exceeded
        mock_log_info.reset_mock()
        mock_sleep.reset_mock()
        mock_create_op.side_effect = RetriesExceeded()
        mock_get_status.side_effect = None

        with self.assertRaises(SystemExit) as exc:
            self.assertIsNone(self.br.restore_backup())

        self.assertEqual(exc.exception.code, BarmanRecoverExitCode.HTTP_ERROR)
        mock_log_info.assert_not_called()
        mock_log_error.assert_called_once_with("An issue was faced while trying to create a recovery operation: %r",
                                               mock_create_op.side_effect)
        mock_sleep.assert_not_called()

        # get status retries exceeded
        mock_create_op.reset_mock()
        mock_create_op.side_effect = None
        mock_log_error.reset_mock()
        mock_log_info.reset_mock()
        mock_get_status.side_effect = RetriesExceeded

        with self.assertRaises(SystemExit) as exc:
            self.assertIsNone(self.br.restore_backup())

        self.assertEqual(exc.exception.code, BarmanRecoverExitCode.HTTP_ERROR)
        mock_log_info.assert_called_once_with("Created the recovery operation with ID %s", "some_id")
        mock_log_error.assert_called_once_with("Maximum number of retries exceeded, exiting.")
        mock_sleep.assert_not_called()


class TestBarmanRecoverCli(unittest.TestCase):

    @patch("logging.error")
    @patch("logging.info")
    @patch("patroni.scripts.barman.recover.BarmanRecover")
    def test_run_barman_recover(self, mock_br, mock_log_info, mock_log_error):
        args = MagicMock()

        # successful execution
        mock_br.return_value.restore_backup.return_value = True

        with self.assertRaises(SystemExit) as exc:
            run_barman_recover(args)

        mock_br.assert_called_once_with(args.barman_server, args.backup_id,
                                        args.ssh_command, args.data_directory,
                                        args.loop_wait, args.api_url,
                                        args.cert_file, args.key_file,
                                        args.retry_wait, args.max_retries)
        mock_log_info.assert_called_once_with("Recovery operation finished successfully.")
        mock_log_error.assert_not_called()
        self.assertEqual(exc.exception.code, BarmanRecoverExitCode.RECOVERY_DONE)

        # failed execution
        mock_br.reset_mock()
        mock_log_info.reset_mock()

        mock_br.return_value.restore_backup.return_value = False

        with self.assertRaises(SystemExit) as exc:
            run_barman_recover(args)

        mock_br.assert_called_once_with(args.barman_server, args.backup_id,
                                        args.ssh_command, args.data_directory,
                                        args.loop_wait, args.api_url,
                                        args.cert_file, args.key_file,
                                        args.retry_wait, args.max_retries)
        mock_log_info.assert_not_called()
        mock_log_error.assert_called_once_with("Recovery operation failed.")
        self.assertEqual(exc.exception.code, BarmanRecoverExitCode.RECOVERY_FAILED)


# stuff from patroni.scripts.barman.config_switch


class TestBarmanConfigSwitch(unittest.TestCase):

    @patch("patroni.scripts.barman.config_switch.PgBackupApi", MagicMock())
    def setUp(self):
        self.bcs = BarmanConfigSwitch(BARMAN_SERVER, BARMAN_SERVER, None, API_URL, None, None, RETRY_WAIT, MAX_RETRIES)
        # Reset the mock as the same instance is used across tests
        self.bcs._api._http.request.reset_mock()
        self.bcs._api._http.request.side_effect = None

    @patch("logging.error")
    @patch("patroni.scripts.barman.config_switch.PgBackupApi")
    def test___init__(self, mock_api, mock_log_error):
        # valid init -- sample 1
        BarmanConfigSwitch(BARMAN_SERVER, BARMAN_MODEL, None, API_URL, None, None, RETRY_WAIT, MAX_RETRIES)

        mock_log_error.assert_not_called()
        mock_api.assert_called_once_with(API_URL, None, None, RETRY_WAIT, MAX_RETRIES)

        # valid init -- sample 2
        mock_api.reset_mock()

        BarmanConfigSwitch(BARMAN_SERVER, None, True, API_URL, None, None, RETRY_WAIT, MAX_RETRIES)

        mock_log_error.assert_not_called()
        mock_api.assert_called_once_with(API_URL, None, None, RETRY_WAIT, MAX_RETRIES)

        # invalid init -- sample 1
        mock_api.reset_mock()

        with self.assertRaises(SystemExit) as exc:
            BarmanConfigSwitch(BARMAN_SERVER, BARMAN_MODEL, True, API_URL, None, None, RETRY_WAIT, MAX_RETRIES)

        mock_log_error.assert_called_once_with("One, and only one among 'barman_model' ('%s') and 'reset' "
                                               "('%s') should be given", BARMAN_MODEL, True)
        mock_api.assert_not_called()
        self.assertEqual(exc.exception.code, BarmanConfigSwitchExitCode.INVALID_ARGS)

        # invalid init -- sample 2
        mock_log_error.reset_mock()
        mock_api.reset_mock()

        with self.assertRaises(SystemExit) as exc:
            BarmanConfigSwitch(BARMAN_SERVER, None, None, API_URL, None, None, RETRY_WAIT, MAX_RETRIES)

        mock_log_error.assert_called_once_with("One, and only one among 'barman_model' ('%s') and 'reset' "
                                               "('%s') should be given", None, None)
        mock_api.assert_not_called()
        self.assertEqual(exc.exception.code, BarmanConfigSwitchExitCode.INVALID_ARGS)

    @patch("time.sleep")
    @patch("logging.info")
    @patch("logging.error")
    def test_switch_config(self, mock_log_error, mock_log_info, mock_sleep):
        mock_create_op = self.bcs._api.create_config_switch_operation
        mock_get_status = self.bcs._api.get_operation_status

        # successful config-switch
        mock_create_op.return_value = "some_id"
        mock_get_status.return_value = OperationStatus.DONE

        self.assertTrue(self.bcs.switch_config())

        mock_create_op.assert_called_once_with(BARMAN_SERVER, BARMAN_SERVER, None)
        mock_get_status.assert_called_once_with(BARMAN_SERVER, "some_id")
        mock_log_info.assert_called_once_with("Created the config switch operation with ID %s", "some_id")
        mock_log_error.assert_not_called()
        mock_sleep.assert_not_called()

        # failed config-switch
        mock_create_op.reset_mock()
        mock_get_status.reset_mock()
        mock_log_info.reset_mock()
        mock_sleep.reset_mock()
        mock_get_status.side_effect = None
        mock_get_status.return_value = OperationStatus.FAILED

        self.assertFalse(self.bcs.switch_config())

        mock_create_op.assert_called_once()
        mock_get_status.assert_called_once_with(BARMAN_SERVER, "some_id")
        mock_log_info.assert_called_once_with("Created the config switch operation with ID %s", "some_id")
        mock_log_error.assert_not_called()
        mock_sleep.assert_not_called()

        # create retries exceeded
        mock_log_info.reset_mock()
        mock_sleep.reset_mock()
        mock_create_op.side_effect = RetriesExceeded()
        mock_get_status.side_effect = None

        with self.assertRaises(SystemExit) as exc:
            self.assertIsNone(self.bcs.switch_config())

        self.assertEqual(exc.exception.code, BarmanConfigSwitchExitCode.HTTP_ERROR)
        mock_log_info.assert_not_called()
        mock_log_error.assert_called_once_with("An issue was faced while trying to create a config switch operation: "
                                               "%r",
                                               mock_create_op.side_effect)
        mock_sleep.assert_not_called()

        # get status retries exceeded
        mock_create_op.reset_mock()
        mock_create_op.side_effect = None
        mock_log_error.reset_mock()
        mock_log_info.reset_mock()
        mock_get_status.side_effect = RetriesExceeded

        with self.assertRaises(SystemExit) as exc:
            self.assertIsNone(self.bcs.switch_config())

        self.assertEqual(exc.exception.code, BarmanConfigSwitchExitCode.HTTP_ERROR)
        mock_log_info.assert_called_once_with("Created the config switch operation with ID %s", "some_id")
        mock_log_error.assert_called_once_with("Maximum number of retries exceeded, exiting.")
        mock_sleep.assert_not_called()


class TestBarmanConfigSwitchCli(unittest.TestCase):

    def test__should_skip_switch(self):
        args = MagicMock()

        for role, switch_when, expected in [
            ("master", "promoted", False),
            ("master", "demoted", True),
            ("master", "always", False),

            ("primary", "promoted", False),
            ("primary", "demoted", True),
            ("primary", "always", False),

            ("promoted", "promoted", False),
            ("promoted", "demoted", True),
            ("promoted", "always", False),

            ("standby_leader", "promoted", True),
            ("standby_leader", "demoted", True),
            ("standby_leader", "always", False),

            ("replica", "promoted", True),
            ("replica", "demoted", False),
            ("replica", "always", False),

            ("demoted", "promoted", True),
            ("demoted", "demoted", False),
            ("demoted", "always", False),
        ]:
            args.role = role
            args.switch_when = switch_when
            self.assertEqual(_should_skip_switch(args), expected)

    @patch("logging.error")
    @patch("logging.info")
    @patch("patroni.scripts.barman.config_switch._should_skip_switch")
    @patch("patroni.scripts.barman.config_switch.BarmanConfigSwitch")
    def test_run_barman_config_switch(self, mock_bcs, mock_skip, mock_log_info, mock_log_error):
        args = MagicMock()

        # successful execution
        mock_skip.return_value = False
        mock_bcs.return_value.switch_config.return_value = True

        with self.assertRaises(SystemExit) as exc:
            run_barman_config_switch(args)

        mock_bcs.assert_called_once_with(args.barman_server, args.barman_model,
                                         args.reset, args.api_url,
                                         args.cert_file, args.key_file,
                                         args.retry_wait, args.max_retries)
        mock_log_info.assert_called_once_with("Config switch operation finished successfully.")
        mock_log_error.assert_not_called()
        self.assertEqual(exc.exception.code, BarmanConfigSwitchExitCode.CONFIG_SWITCH_DONE)

        # failed execution
        mock_bcs.reset_mock()
        mock_log_info.reset_mock()

        mock_bcs.return_value.switch_config.return_value = False

        with self.assertRaises(SystemExit) as exc:
            run_barman_config_switch(args)

        mock_bcs.assert_called_once_with(args.barman_server, args.barman_model,
                                        args.reset, args.api_url,
                                        args.cert_file, args.key_file,
                                        args.retry_wait, args.max_retries)
        mock_log_info.assert_not_called()
        mock_log_error.assert_called_once_with("Config switch operation failed.")
        self.assertEqual(exc.exception.code, BarmanConfigSwitchExitCode.CONFIG_SWITCH_FAILED)

        # skipped execution
        mock_bcs.reset_mock()
        mock_log_info.reset_mock()
        mock_log_error.reset_mock()
        mock_skip.return_value = True

        with self.assertRaises(SystemExit) as exc:
            run_barman_config_switch(args)

        mock_bcs.assert_not_called()
        mock_log_info.assert_called_once_with("Config switch operation was skipped (role=%s, "
                                              "switch_when=%s).", args.role, args.switch_when)
        mock_log_error.assert_not_called()
        self.assertEqual(exc.exception.code, BarmanConfigSwitchExitCode.CONFIG_SWITCH_SKIPPED)


# stuff from patroni.scripts.barman.cli


class TestMain(unittest.TestCase):

    @patch("patroni.scripts.barman.cli.set_up_logging")
    @patch("patroni.scripts.barman.cli.ArgumentParser")
    def test_main(self, mock_arg_parse, mock_set_up_log):
        # sub-command specified
        args = MagicMock()
        mock_arg_parse.return_value.parse_known_args.return_value = (args, None)

        main()

        mock_arg_parse.assert_called_once()
        mock_set_up_log.assert_called_once_with(args.log_file)
        mock_arg_parse.return_value.print_help.assert_not_called()
        args.func.assert_called_once_with(args)

        # sub-command not specified
        mock_arg_parse.reset_mock()
        mock_set_up_log.reset_mock()
        delattr(args, "func")

        with self.assertRaises(SystemExit) as exc:
            main()

        mock_arg_parse.assert_called_once()
        mock_set_up_log.assert_called_once_with(args.log_file)
        mock_arg_parse.return_value.print_help.assert_called_once_with()
        self.assertEqual(exc.exception.code, -1)
