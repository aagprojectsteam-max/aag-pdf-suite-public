"""Independent structural checks and bounded-resolution page rendering."""
import math
from pathlib import Path
import subprocess
from .platforms import qpdf_executable, IS_WINDOWS


class ValidationError(ValueError):
    pass


def check_tree(pdf):
    import pikepdf
    root = pdf.Root
    if root.get("/Type") != pikepdf.Name.Catalog or "/Pages" not in root:
        raise ValidationError("Invalid catalog")
    seen = set()

    def visit(node, parent=None, depth=0):
        if depth > 256 or not node.is_indirect or node.objgen in seen:
            raise ValidationError("Page tree cycle, duplicate, direct node, or excessive depth")
        seen.add(node.objgen)
        if parent is not None and node.get("/Parent") != parent:
            raise ValidationError("Page tree parent mismatch")
        kind = node.get("/Type")
        if kind == pikepdf.Name.Page:
            return 1
        if kind != pikepdf.Name.Pages or "/Kids" not in node:
            raise ValidationError("Invalid page tree node")
        count = sum(visit(child, node, depth + 1) for child in node.Kids)
        if count != node.get("/Count"):
            raise ValidationError("Page tree count mismatch")
        return count

    count = visit(root.Pages)
    if count < 1:
        raise ValidationError("Empty document")
    return count


def validate(path: Path, all_pages=False, *, font_policy='strict', progress=None):
    import pikepdf
    import pymupdf
    with path.open("rb") as src:
        if not src.read(9).startswith(b"%PDF-"):
            raise ValidationError("Missing normal PDF header")
    try:
        if progress:progress({'phase':'structure','done':0,'total':0})
        with pikepdf.open(path, attempt_recovery=False, inherit_page_attributes=False) as pdf:
            if pdf.is_encrypted:
                raise ValidationError("Encrypted PDF is unsupported")
            count = check_tree(pdf)
            if len(pdf.pages) != count:
                raise ValidationError("Page enumeration mismatch")
            for page in pdf.pages:
                if len(page.mediabox) != 4:
                    raise ValidationError("Invalid page dimensions")
            warnings = pdf.check_pdf_syntax()
            if warnings:
                raise ValidationError("; ".join(warnings)[:2000])
        qpdf = qpdf_executable()
        warning = ""
        if qpdf:
            # communicate in bounded intervals so a caller can cancel
            # and reap qpdf as well as its Python worker.
            with subprocess.Popen([qpdf, "--check", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                import time
                deadline=time.monotonic()+180
                try:
                    while True:
                        try:
                            stdout,stderr=child.communicate(timeout=.1)
                            break
                        except subprocess.TimeoutExpired:
                            if progress:progress({'phase':'structure','done':0,'total':count})
                            if time.monotonic()>deadline:raise ValidationError('qpdf validation timeout')
                    check=subprocess.CompletedProcess(child.args,child.returncode,stdout,stderr)
                finally:
                    if child.poll() is None:child.kill();child.communicate()
            if check.returncode:
                raise ValidationError((check.stdout + check.stderr)[-2000:])
        else:
            warning = ("Windows validation uses bundled libqpdf strict parsing/syntax checks plus MuPDF" if IS_WINDOWS
                       else "qpdf executable unavailable; libqpdf strict parsing and syntax checks used")
        # Flush the native repetition counter before clearing its Python buffer.
        # Clearing only the buffer can leave an orphan "repeated N times" warning
        # after earlier renders in this same long-lived worker.
        pymupdf.TOOLS.mupdf_warnings()
        with pymupdf.open(path) as pdf:
            if pdf.is_encrypted or pdf.is_repaired or pdf.page_count != count:
                raise ValidationError("Independent parser rejected structure or page count")
            selected = range(count) if all_pages else sorted({0, count // 2, count - 1})
            for index in selected:
                if progress:progress({'phase':'render_validation','done':index,'total':count})
                page = pdf[index]
                size = max(page.rect.width, page.rect.height)
                if not math.isfinite(size) or size <= 0:
                    raise ValidationError("Invalid page dimensions")
                scale = min(1.0, 1200 / size)
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                if pixmap.width < 1 or pixmap.height < 1:
                    raise ValidationError(f"Page {index + 1} failed to render")
                del pixmap
            if progress:progress({'phase':'render_validation','done':count,'total':count})
        warnings = pymupdf.TOOLS.mupdf_warnings()
        from .render_diagnostics import classify
        if not classify(warnings, font_policy):
            raise ValidationError(warnings[:2000])
        if warnings:
            warning = (warning + '\nFont diagnostics retained: ' + warnings).strip()
        return count, warning
    except ValidationError:
        raise
    except (ImportError, ModuleNotFoundError):
        raise
    except Exception as exc:
        raise ValidationError(str(exc)) from exc
