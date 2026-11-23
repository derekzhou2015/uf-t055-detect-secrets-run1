import inspect
import sys
from functools import lru_cache
from typing import Any
from typing import Iterable
from typing import List
from typing import Optional
from typing import TypeVar

from ..types import NamedIO
from ..util.importlib import import_types_from_package
from .base import BaseTransformer
from .exceptions import ParsingError


Transformer = TypeVar('Transformer', bound=BaseTransformer)


def get_transformed_file(
    file: NamedIO,
    use_eager_transformers: bool = False,
) -> Optional[List[str]]:
    decode_error = None
    for transformer in get_transformers():
        if not transformer.should_parse_file(file.name):
            continue

        if use_eager_transformers != transformer.is_eager:
            continue

        try:
            return transformer.parse_file(file)
        except ParsingError:
            pass
        except UnicodeDecodeError as error:
            # A single transformer may not be able to decode the file (e.g. a legacy
            # encoding). Don't discard the whole file because of it: let the other
            # transformers, and ultimately the raw line reader, have a chance instead.
            decode_error = error
        finally:
            file.seek(0)

    if decode_error is not None:
        # Every applicable transformer failed to decode the file. Re-raise so the
        # caller can apply its binary-file handling, rather than silently returning
        # nothing and dropping the file's secrets.
        raise decode_error

    return None


@lru_cache(maxsize=1)
def get_transformers() -> Iterable[Transformer]:
    return [
        item()
        for item in import_types_from_package(
            sys.modules[__name__],
            filter=lambda x: not _is_valid_transformer(x),
        )
    ]


def _is_valid_transformer(attribute: Any) -> bool:
    return (
        inspect.isclass(attribute)
        and issubclass(attribute, BaseTransformer)
        and attribute.__name__ != 'BaseTransformer'
    )
