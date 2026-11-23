"""
Regression tests for two silent under-reporting defects:

1. On non-UTF-8 locales (e.g. Windows cp936/GBK), files containing non-ASCII
   UTF-8 characters raised UnicodeDecodeError while being read and were silently
   skipped in their entirety, dropping every secret they held. Text scanning now
   decodes UTF-8 first and falls back to the locale encoding for legacy files.

2. The command-line entry point silently assembled an extra false-positive
   filter (the gibberish filter, whenever its optional dependency happened to be
   installed) that the library entry point did not, so the CLI reported fewer
   hits than the library for the same file. The filter is now opt-in, leaving the
   default filter set identical across both entry points.
"""
import contextlib
import io
import json
import os

import pytest

from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.main import main as cli_main
from detect_secrets.settings import default_settings
from detect_secrets.settings import get_settings
from detect_secrets.util import git
from detect_secrets.util import encoding as encoding_util


# A non-ASCII (CJK) comment followed by lines carrying real secrets.
UTF8_FILE_CONTENT = (
    '# 中文注释：演示含非 ASCII 字符的 UTF-8 源文件\n'
    'aws_access_key = "AKIAIOSFODNN7EXAMPLE"\n'
    'app_secret = "ToCynx5Se4e2PtoZxEhW7lUJcOX15c54"\n'
)
SECRET_VALUE = 'ToCynx5Se4e2PtoZxEhW7lUJcOX15c54'
GIBBERISH_FILTER = 'detect_secrets.filters.gibberish.should_exclude_secret'


@pytest.fixture
def utf8_secret_file(tmp_path):
    path = tmp_path / 'sample.py'
    path.write_bytes(UTF8_FILE_CONTENT.encode('utf-8'))
    return str(path)


@pytest.fixture
def repo_local_file():
    """
    The CLI only enumerates files inside the working tree. Place a uniquely named
    scratch file under the repository's test_data directory and clean it up after.
    """
    filename = os.path.join(
        git.get_root_directory(),
        'test_data',
        '_t055_regression_sample.py',
    )
    with open(filename, 'wb') as handle:
        handle.write(UTF8_FILE_CONTENT.encode('utf-8'))

    try:
        yield os.path.relpath(filename)
    finally:
        os.remove(filename)


def _library_secret_values(path):
    with default_settings():
        collection = SecretsCollection()
        collection.scan_file(path)
    return {
        secret.secret_value
        for secrets in collection.data.values()
        for secret in secrets
    }


def _run_cli(path, *extra):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli_main(['scan', '--no-verify', *extra, path])
    return json.loads(buffer.getvalue())


class TestUtf8NonAsciiFilesAreNotSilentlyDropped:
    @staticmethod
    def test_utf8_file_scannable_under_non_utf8_locale(utf8_secret_file, monkeypatch):
        # Force a non-UTF-8 preferred encoding regardless of the host's locale,
        # so this reproduces the gap deterministically on any machine.
        monkeypatch.setattr(
            encoding_util.locale,
            'getpreferredencoding',
            lambda _do_not_use_caching=False: 'cp936',
        )

        # Sanity check: under cp936 the plain, locale-based read really does fail,
        # which is exactly what used to make the scanner drop the whole file.
        with pytest.raises(UnicodeDecodeError):
            open(utf8_secret_file, encoding='cp936').read()

        assert SECRET_VALUE in _library_secret_values(utf8_secret_file)

    @staticmethod
    def test_legacy_locale_encoded_file_still_scans(tmp_path, monkeypatch):
        # The locale fallback keeps genuinely legacy-encoded text files working.
        legacy = tmp_path / 'legacy.env'
        legacy.write_bytes(
            ('# 注释\napp_secret = "%s"\n' % SECRET_VALUE).encode('gbk'),
        )
        monkeypatch.setattr(
            encoding_util.locale,
            'getpreferredencoding',
            lambda _do_not_use_caching=False: 'gbk',
        )

        assert SECRET_VALUE in _library_secret_values(str(legacy))

    @staticmethod
    def test_repeated_scans_are_deterministic(utf8_secret_file, monkeypatch):
        monkeypatch.setattr(
            encoding_util.locale,
            'getpreferredencoding',
            lambda _do_not_use_caching=False: 'cp936',
        )

        runs = [_library_secret_values(utf8_secret_file) for _ in range(3)]
        assert runs[0] == runs[1] == runs[2]
        assert SECRET_VALUE in runs[0]


class TestCliAndLibraryEntryPointsAreAligned:
    @staticmethod
    def test_cli_does_not_silently_enable_gibberish_filter(repo_local_file):
        baseline = _run_cli(repo_local_file)

        enabled = {entry['path'] for entry in baseline['filters_used']}
        assert GIBBERISH_FILTER not in enabled
        assert GIBBERISH_FILTER not in get_settings().filters

    @staticmethod
    def test_cli_reports_every_secret_the_library_reports(repo_local_file):
        library_values = _library_secret_values(repo_local_file)
        assert library_values  # the library genuinely finds something

        baseline = _run_cli(repo_local_file)
        cli_hits = {
            (entry['type'], entry['line_number'])
            for entries in baseline['results'].values()
            for entry in entries
        }

        with default_settings():
            collection = SecretsCollection()
            collection.scan_file(repo_local_file)
            library_hits = {
                (secret.type, secret.line_number)
                for secrets in collection.data.values()
                for secret in secrets
            }

        assert library_hits <= cli_hits

    @staticmethod
    def test_gibberish_filter_remains_available_via_explicit_opt_in(repo_local_file):
        baseline = _run_cli(repo_local_file, '--use-gibberish')

        enabled = {entry['path'] for entry in baseline['filters_used']}
        assert GIBBERISH_FILTER in enabled
        assert GIBBERISH_FILTER in get_settings().filters

    @staticmethod
    def test_custom_gibberish_flags_imply_opt_in(repo_local_file):
        # Supplying a model/limit only makes sense with the filter active, so those
        # flags keep enabling it even without --use-gibberish.
        model = os.path.join(
            os.path.dirname(__file__),
            '..', '..',
            'detect_secrets', 'filters', 'gibberish', 'rfc.model',
        )
        baseline = _run_cli(repo_local_file, '--gibberish-model', os.path.abspath(model))

        enabled = {entry['path'] for entry in baseline['filters_used']}
        assert GIBBERISH_FILTER in enabled
