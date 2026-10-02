"""Text preprocessing shared by training and inference.

Any change here changes what the model sees, so bump ``PREPROCESS_VERSION``;
saved models record the version they were trained with and refuse to load
under a different one.
"""

from __future__ import annotations

import re

PREPROCESS_VERSION = 1
MAX_CHARS = 6000

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_URL = re.compile(r"https?://\S+")
_WHITESPACE = re.compile(r"\s+")


def build_text(title: str | None, body: str | None) -> str:
    """Combine an issue's title and body into one normalised string.

    * HTML comments (issue-template instructions) are removed.
    * Fenced code blocks become the token ``codeblock`` — their presence is a
      useful signal, their contents (paths, variable names) mostly noise.
    * URLs become ``urltoken``.
    * The title is included twice, giving it more weight than the body.
    * Lower-cased, whitespace-collapsed, and capped at ``MAX_CHARS``.
    """
    title = title or ""
    body = _HTML_COMMENT.sub(" ", body or "")
    body = _CODE_FENCE.sub(" codeblock ", body)
    text = f"{title} {title} {body}"
    text = _URL.sub(" urltoken ", text)
    return _WHITESPACE.sub(" ", text).strip().lower()[:MAX_CHARS]
