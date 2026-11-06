import json
import os
from pathlib import Path


STATUS_LABELS = {

    'DEPENDENCY_MISSING': 'רכיב תוכנה נדרש אינו מותקן; יש להריץ בדיקת סביבה',
    'RUNTIME_LOAD_FAILED': 'רכיב תוכנה לא נטען; יש לבדוק את ההתקנה',
    'LIMIT_EXCEEDED': 'הפעולה חרגה ממגבלות המשאבים',

    "PASS_DECODED": "הושלם — PDF חולץ ואומת",
    "NEW_VARIANT_DETECTED": "נדרש תיקון מבני; מצב מדויק אינו מאפשר אותו",
    "UNSUPPORTED_AFTER_ANALYSIS": "הניתוח הושלם ללא מסלול שחזור בטוח",
    "PASS_EXACT": "הושלם — תוכן המקור נשמר במדויק", "PASS_REPAIRED": "הושלם — מבנה שוחזר עם מגבלות מתועדות",
    "SKIPPED_EXISTING_VALID": "דולג — כבר אומת",
    "FAIL_INVALID_SOURCE": "נכשל — קובץ מקור לא תקין", "FAIL_REPAIR": "נכשל — לא ניתן להשלים תיקון",
    "FAIL_VALIDATION": "נכשל — בדיקת PDF לא עברה",
    "UNSUPPORTED_OR_AMBIGUOUS": "לא נתמך — מבנה קובץ לא חד־משמעי", "CANCELLED": "בוטל",
}


class Report:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.json = path.open("x", encoding="utf-8")
        try:
            self.log = path.with_suffix(path.suffix + ".log").open("x", encoding="utf-8")
        except Exception:
            self.json.close()
            raise

    def write(self, result):
        self.json.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        # repr prevents newline-containing filenames from impersonating log records.
        self.log.write(f"{result.timestamp} {result.status} {result.source_path!r}: {result.error or STATUS_LABELS.get(result.status,result.status)}\n")
        if result.warning:
            self.log.write(f'  Warning: {result.warning}\n')
        if result.assumptions:
            self.log.write(f'  Assumptions: {result.assumptions!r}\n')
        if result.affected_pages:
            self.log.write(f'  Affected pages: {result.affected_pages!r}\n')
        for item in result.reconstructed_objects:
            if isinstance(item, dict):
                self.log.write(f"  Reconstructed object {item.get('object', 'document structure')}: {item.get('replacement')!r}; {item.get('uncertainty', '')}\n")
            else:
                # Earlier generic provenance used descriptive strings. Retain
                # their content when exporting or reopening those PDFs.
                self.log.write(f"  Reconstructed structure: {item!r}; see recovery provenance and assumptions\n")
        for stream in (self.json, self.log):
            stream.flush()
            os.fsync(stream.fileno())

    def finish(self, summary):
        self.json.write(json.dumps({"type": "summary", **summary}, ensure_ascii=False) + "\n")
        self.log.write(" ".join(f"{key}={value}" for key, value in summary.items()) + "\n")
        for stream in (self.json, self.log):
            stream.flush()
            os.fsync(stream.fileno())

    def close(self):
        self.json.close()
        self.log.close()
