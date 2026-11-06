"""Narrow warning policy; retains observed font diagnostics without font changes."""
import re

FONT_WARNINGS = {'freetype could not find any cmaps', 'font has no ascender or descender'}


def classify(warnings, policy='strict'):
    lines = [line.strip() for line in warnings.splitlines() if line.strip()]
    if not lines:
        return True
    if policy != 'report-fonts':
        return False
    previous_font = False
    for line in lines:
        if line in FONT_WARNINGS:
            previous_font = True
        elif previous_font and re.fullmatch(r'\.\.\. repeated \d+ times\.\.\.', line):
            continue
        else:
            return False
    return True
