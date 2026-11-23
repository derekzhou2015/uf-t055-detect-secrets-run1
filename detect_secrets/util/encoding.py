import codecs
import locale
from typing import List
from typing import Optional
from typing import Tuple

from ..types import NamedIO


# Source/config files scanned for secrets are overwhelmingly UTF-8. On systems whose
# locale encoding is not UTF-8 (e.g. Windows using cp936/GBK), opening such files with
# the platform default encoding raises UnicodeDecodeError. The scan pipeline treated
# that error as "binary file" and silently skipped the *entire* file, dropping every
# secret it contained. Decoding as UTF-8 first keeps such files scannable, while the
# locale fallback preserves support for genuinely legacy-encoded text files.
UTF8 = 'utf-8'


def get_preferred_text_encodings() -> List[str]:
    """
    Returns the encodings to attempt when reading scanned files as text, in order.

    UTF-8 is always tried first. The platform's preferred encoding is used as a
    fallback for legacy (non UTF-8) text files. Duplicate encodings are collapsed.
    """
    seen = set()
    output = []
    for encoding in (UTF8, locale.getpreferredencoding(False)):
        if not encoding:
            continue

        # Normalize aliases (e.g. UTF8 -> utf-8) so we don't attempt the same codec twice.
        try:
            encoding = codecs.lookup(encoding).name
        except LookupError:
            continue

        if encoding in seen:
            continue

        seen.add(encoding)
        output.append(encoding)

    return output


def read_text_lines(filename: str) -> Tuple[NamedIO, List[str]]:
    """
    Opens ``filename`` and reads all of its lines as text, trying each preferred
    encoding in turn (UTF-8 first, then the platform locale encoding).

    The returned open handle is rewound to the start of the file, so transformers
    and the eager-scan path can re-read it.

    :raises: IOError when the file cannot be opened.
    :raises: UnicodeDecodeError when no supported encoding can decode the file
        (i.e. it is a binary file).
    """
    last_error: Optional[UnicodeDecodeError] = None
    for encoding in get_preferred_text_encodings():
        handle = open(filename, encoding=encoding)
        try:
            lines = handle.readlines()
        except UnicodeDecodeError as error:
            handle.close()
            last_error = error
            continue

        handle.seek(0)
        return handle, lines  # type: ignore[return-value]

    # get_preferred_text_encodings() always yields at least UTF-8, so a failure
    # here means every text codec rejected the file: it is a binary file.
    assert last_error is not None
    raise last_error
