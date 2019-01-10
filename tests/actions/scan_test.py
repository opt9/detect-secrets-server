from __future__ import absolute_import

import json
import textwrap
from contextlib import contextmanager

import mock
import pytest
from detect_secrets.core.secrets_collection import SecretsCollection

from detect_secrets_server.actions import scan_repo
from detect_secrets_server.core.usage.parser import ServerParserBuilder
from detect_secrets_server.hooks.stdout import StdoutHook
from testing.factories import secrets_collection_factory
from testing.mocks import mock_git_calls
from testing.mocks import SubprocessMock


class TestScanRepo(object):

    @staticmethod
    def parse_args(argument_string=''):
        base_argument = (
            'scan will_be_mocked '
            '--output-hook examples/standalone_hook.py '
        )

        with mock.patch(
            'detect_secrets_server.core.usage.s3.should_enable_s3_options',
            return_value=False,
        ):
            return ServerParserBuilder().parse_args(
                (base_argument + argument_string).split()
            )

    @contextmanager
    def setup_env(self, scan_results, argument_string=''):
        """This sets up the relevant mocks, so that we can conduct testing."""
        args = self.parse_args(argument_string)

        with mock.patch(
            'detect_secrets_server.repos.base_tracked_repo.BaseTrackedRepo.scan',
            return_value=scan_results,
        ), mock.patch(
            # We mock this, so that we can successfully load_from_file
            'detect_secrets_server.storage.file.FileStorage.get',
            return_value=mock_tracked_file('old_sha'),
        ):
            yield args

    def test_quits_early_if_cannot_load_meta_tracking_file(self):
        args = self.parse_args()

        assert scan_repo(args) == 1

    def test_updates_tracked_repo_when_no_secrets_are_found(
        self,
        mock_file_operations,
        mock_logger
    ):
        # mock_git_calls is used for repo.update
        with self.setup_env(
            SecretsCollection()
        ) as args, mock_git_calls(
            SubprocessMock(
                expected_input='git rev-parse HEAD',
                mocked_output='new_sha',
            ),
        ):
            assert scan_repo(args) == 0

        mock_logger.info.assert_called_with(
            'No secrets found for %s',
            'yelp/detect-secrets',
        )

        mock_file_operations.write.assert_called_with(
            json.dumps(mock_tracked_file('new_sha'), indent=2, sort_keys=True)
        )

    def test_alerts_on_secrets_found(
        self,
        mock_file_operations,
        mock_logger,
    ):
        secrets = secrets_collection_factory([
            {
                'filename': 'file_with_secrets',
                'lineno': 5,
            },
        ])

        with self.setup_env(
            secrets,
        ) as args, mock_git_calls(
            # First, we get the main branch
            SubprocessMock(
                expected_input='git rev-parse --abbrev-ref HEAD',
                mocked_output='master',
            ),

            # then, we get the blame info for that branch.
            SubprocessMock(
                expected_input=(
                    'git blame master -L 5,5 --show-email '
                    '--line-porcelain -- file_with_secrets'
                ),
                mocked_output=mock_blame_info(),
            ),
        ):
            secret_hash = list(
                secrets.data['file_with_secrets'].values()
            )[0].secret_hash

            args.output_hook = mock_external_hook(
                'yelp/detect-secrets',
                {
                    'file_with_secrets': [{
                        'type': 'type',
                        'hashed_secret': secret_hash,
                        'line_number': 5,
                        'author': 'khock',
                    }],
                },
            )

            assert scan_repo(args) == 0

        mock_logger.error.assert_called_with(
            'Secrets found in %s',
            'yelp/detect-secrets',
        )
        assert not mock_file_operations.write.called

    def test_does_not_write_state_when_dry_run(self, mock_file_operations):
        with self.setup_env(
            SecretsCollection(),
            '--dry-run',
        ) as args:
            assert scan_repo(args) == 0

        assert not mock_file_operations.write.called


def mock_tracked_file(sha):
    return {
        'sha': sha,
        'repo': 'git@github.com:yelp/detect-secrets',
        'plugins': {
            'HexHighEntropyString': {
                'hex_limit': 3,
            },
            'Base64HighEntropyString': {
                'base64_limit': 4.5,
            },
            'PrivateKeyDetector': {},
            'BasicAuthDetector': {},
        },
        'crontab': '* * 4 * *',
        'baseline_filename': None,
        'exclude_regex': None,
    }


def mock_blame_info():
    return textwrap.dedent("""
        d39c008353447bbc1845812fcaf0a03b50af439f 177 174 1
        author Kevin Hock
        author-mail <khock@yelp.com>
        author-time 1513196047
        author-tz -0800
        committer Foo
        committer-mail <foo@example.com>
        committer-time 1513196047
        committer-tz -0800
        summary mock
        previous 23c630620c23843559485fd2ada02e9e7bc5a07e4 mock_output.java
        filename some_file.java
        "super:secret f8616fefbo41fdc31960ehef078f85527")));
    """)[1:]


def mock_external_hook(expected_repo_name, expected_secrets):
    def wrapped(repo_name, secrets):
        assert repo_name == expected_repo_name
        assert secrets == expected_secrets

    mock_hook = StdoutHook()
    mock_hook.alert = wrapped

    return mock_hook


@pytest.fixture
def mock_logger():
    with mock.patch(
        'detect_secrets_server.actions.scan.log'
    ) as log:
        yield log