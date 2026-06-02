"""
NotebookEdit — Jupyter notebook cell editor.
Ported from Rust: tools/src/lib.rs execute_notebook_edit().

Edit Jupyter notebook cells without breaking the JSON structure.
"""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class NotebookEditInput:
    notebook_path: str
    new_source: str
    cell_id: str | None = None
    cell_type: str | None = None       # "code" | "markdown" | "raw"
    edit_mode: str | None = None       # "replace" (default) | "insert" | "delete"


@dataclass
class NotebookEditOutput:
    new_source: str
    cell_id: str | None
    cell_type: str | None
    language: str | None
    edit_mode: str
    error: str | None
    notebook_path: str
    original_file: str
    updated_file: str


def notebook_edit(input: NotebookEditInput) -> NotebookEditOutput:
    """
    Edit a Jupyter notebook cell. Mirrors Rust execute_notebook_edit().

    edit_mode:
      - "replace" (default): replace the targeted cell's source
      - "insert": insert a new cell after the targeted cell (or at start when no id)
      - "delete": remove the targeted cell
    """
    nb_path = Path(input.notebook_path).expanduser().resolve()

    if nb_path.suffix != ".ipynb":
        raise ValueError("Notebook path must end in .ipynb")

    original_file = nb_path.read_text(encoding="utf-8")
    nb = json.loads(original_file)
    cells = nb.get("cells", [])

    edit_mode = input.edit_mode or "replace"
    target_index = _resolve_cell_index(cells, input.cell_id)
    language = _detect_language(nb)

    if edit_mode == "delete":
        if target_index is None:
            raise IndexError("Cell index out of range")
        cells.pop(target_index)
    elif edit_mode == "insert":
        new_cell = _build_cell(
            cell_type=input.cell_type or "code",
            source=input.new_source,
        )
        insert_at = (target_index + 1) if target_index is not None else 0
        cells.insert(insert_at, new_cell)
        target_index = insert_at
    else:  # replace
        if target_index is None:
            raise IndexError("Cell index out of range")
        cell = cells[target_index]
        cell["source"] = _split_source(input.new_source)
        if input.cell_type is not None:
            cell["cell_type"] = input.cell_type
        if cell.get("cell_type") == "code" and "outputs" not in cell:
            cell["outputs"] = []
        if cell.get("cell_type") == "code" and "execution_count" not in cell:
            cell["execution_count"] = None

    nb["cells"] = cells
    updated_file = json.dumps(nb, indent=2, ensure_ascii=False)
    nb_path.write_text(updated_file, encoding="utf-8")

    cell_id = (
        cells[target_index].get("id") if target_index is not None and target_index < len(cells)
        else None
    )
    cell_type = (
        cells[target_index].get("cell_type") if target_index is not None and target_index < len(cells)
        else input.cell_type
    )

    return NotebookEditOutput(
        new_source=input.new_source,
        cell_id=cell_id,
        cell_type=cell_type,
        language=language,
        edit_mode=edit_mode,
        error=None,
        notebook_path=str(nb_path),
        original_file=original_file,
        updated_file=updated_file,
    )


def _resolve_cell_index(cells: list[dict], cell_id: str | None) -> int | None:
    """Mirrors Rust resolve_cell_index(). Accepts numeric index or string id."""
    if cell_id is None:
        return None
    try:
        idx = int(cell_id)
        if 0 <= idx < len(cells):
            return idx
        return None
    except ValueError:
        pass
    for i, cell in enumerate(cells):
        if cell.get("id") == cell_id:
            return i
    return None


def _detect_language(nb: dict) -> str | None:
    metadata = nb.get("metadata", {})
    kernelspec = metadata.get("kernelspec", {})
    language_info = metadata.get("language_info", {})
    return language_info.get("name") or kernelspec.get("language")


def _split_source(source: str) -> list[str]:
    """Notebook cell source is a list of lines preserving line endings."""
    if not source:
        return []
    return source.splitlines(keepends=True)


def _build_cell(cell_type: str, source: str) -> dict:
    cell: dict = {
        "cell_type": cell_type,
        "source": _split_source(source),
        "metadata": {},
    }
    if cell_type == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return cell
