# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================


def chunk_markdown(filepath: str, sep: list[str], max_length: int, overlap: int) -> list[str]:
    """
    Chunk a markdown file recursively by a list of separator.

    Args:
        filepath (str): Path to a markdown file to be chunked.
        sep (list[str]): List of separator to split the document,leftmost element being the first separator to be
            applied.
        max_length (int): Maximum number of characters in each split.
        overlap (int): Number of characters in the overlap.

    Returns:
        List[str], list of document chunks.
    """
    markdown_string = chunk_markdown_by_sep(filepath=filepath, sep=sep)
    markdown_string = refine_by_length(text=markdown_string, max_length=max_length, overlap=overlap)
    return markdown_string


def chunk_markdown_by_sep(filepath: str, sep: list[str]) -> list[str]:
    """
    Chunk a markdown file recursively by a list of separator.

    Args:
        filepath (str): Path to a markdown file to be chunked.
        sep (list[str]): List of separator to split the document, leftmost element being the first separator to be
            applied

    Returns:
        List[str], list of document chunks.
    """
    with open(filepath) as f:
        out = [f.read()]

    for i in sep:
        out = split_by_sep(out, i)

    while "" in out:
        out.remove("")

    return out


def refine_by_length(text: list[str], max_length: int, overlap: int) -> list[str]:
    """
    Refine each document chunk to be no longer than max_length characters.

    Args:
        text (str): Piece of text to be split.
        max_length (int): Maximum number of characters in each split.
        overlap (int): Number of characters in the overlap.

    Returns:
        List[str], list of string after splitting each string element.
    """
    out = []
    for i in text:
        out += split_by_length(i, max_length=max_length, overlap=overlap)

    return out


def split_by_length(text: str, max_length: int, overlap: int) -> list[str]:
    """
    Split each string by fixed length and overlap.

    Args:
        text (str): Piece of text to be split.
        max_length (int): Maximum number of characters in each split.
        overlap (int): Number of characters in the overlap.

    Returns:
        List[str], list of string after splitting each string element
    """
    if max_length <= overlap:
        raise ValueError("Parameter max_length must be strictly larger than overlap.")

    n = len(text)
    left = 0
    right = max_length
    out = [text[left:right]]
    while right - left == max_length and right < n:
        left += max_length - overlap
        right = min(right + max_length - overlap, n)
        out.append(text[left:right])

    return out


def split_by_sep(text: list[str], sep: str) -> list[str]:
    """
    Split each string element in a list with a common separator, and re-concat them into one list.

    Args:
        text (list[str]): List of text to be split.
        sep (str): Separator to be used in text splitting.

    Returns:
        List[str], list of string after splitting each string element.
    """
    out = []
    for i in text:
        out += i.split(sep)

    return out
