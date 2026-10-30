"""
Extra spelling + grammar knowledge for TextCheck.

1. common_misspellings.txt : 52,000+ known misspellings -> fix (from codespell / Wikipedia).
   These are never real words, so they're fixed instantly, before the AI even runs.
   common_grammar.txt       : common grammar mistakes ("should of" -> "should have"), also instant.
2. LanguageTool (optional)  : free open-source grammar checker with thousands of grammar rules.
   It doesn't change your text; it tells Gemma what looks wrong, and Gemma decides.
   Turn on with:  pip install language-tool-python   (needs Java: winget install Microsoft.OpenJDK.21)
"""

import os
import re
import threading

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------- 1. Common misspellings list ----------
MISSPELLINGS = {}
_path = os.path.join(HERE, "common_misspellings.txt")
if os.path.exists(_path):
    with open(_path, encoding="utf-8") as f:
        for line in f:
            if "->" in line and not line.startswith("#"):
                wrong, right = line.strip().split("->", 1)
                MISSPELLINGS[wrong] = right
    print(f"Loaded {len(MISSPELLINGS):,} common misspellings.")
else:
    print("Tip: put common_misspellings.txt next to this file to catch 52,000+ common misspellings.")

MISSPELLINGS.update({"alot": "a lot"})  # texting favorites

# ---------- 1b. Common grammar mistakes list (phrases) ----------
GRAMMAR_RULES = []
_gpath = os.path.join(HERE, "common_grammar.txt")
if os.path.exists(_gpath):
    with open(_gpath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "->" in line and not line.startswith("#"):
                wrong, right = (p.strip() for p in line.split("->", 1))
                pattern = re.compile(r"(?<![\w'])" + re.escape(wrong).replace(r"\ ", r"\s+") + r"(?![\w'])", re.I)
                GRAMMAR_RULES.append((pattern, right))
    print(f"Loaded {len(GRAMMAR_RULES)} common grammar rules.")


def fix_grammar_phrases(text: str, changes: list) -> str:
    """Instantly fix common grammar mistakes like 'should of' -> 'should have'."""
    for pattern, right in GRAMMAR_RULES:
        def repl(m, right=right):
            found = m.group(0)
            fixed = right[0].upper() + right[1:] if found[0].isupper() else right
            changes.append(f'"{found}" -> "{fixed}" (common grammar mistake)')
            return fixed
        text = pattern.sub(repl, text)
    return text


def fix_misspellings(text: str, changes: list, skip=()) -> str:
    """Instantly fix known misspellings like 'definately' -> 'definitely'."""
    def repl(m):
        word = m.group(0)
        if word.lower() in skip:
            return word
        right = MISSPELLINGS.get(word.lower())
        if not right:
            return word
        if word[0].isupper():
            right = right[0].upper() + right[1:]
        changes.append(f'"{word}" -> "{right}" (common misspelling)')
        return right
    text = re.sub(r"(?<![\w'])[A-Za-z]+(?![\w'])", repl, text)
    return fix_grammar_phrases(text, changes)


# ---------- 2. LanguageTool grammar rules (optional) ----------
_tool = None
_tool_status = "off"   # off -> loading -> ready / failed

# Rules that would fight your casual texting style, so we ignore them
IGNORE_RULES = {
    "UPPERCASE_SENTENCE_START", "I_LOWERCASE", "PUNCTUATION_PARAGRAPH_END", "COMMA_PARENTHESIS_WHITESPACE",
    "WHITESPACE_RULE", "EN_QUOTES", "DASH_RULE", "ENGLISH_WORD_REPEAT_BEGINNING_RULE", "SENTENCE_FRAGMENT",
    "MORFOLOGIK_RULE_EN_US",  # spelling: our spell checker + misspellings list already handle it
}
IGNORE_CATEGORIES = {"CASING", "TYPOGRAPHY", "STYLE", "REDUNDANCY", "TYPOS", "PUNCTUATION"}


def _load_languagetool():
    global _tool, _tool_status
    try:
        import language_tool_python
        print("Starting LanguageTool grammar checker (first time downloads ~250 MB)...")
        _tool = language_tool_python.LanguageTool("en-US")
        _tool.check("Warm up.")  # first check is slow, do it now instead of during your first message
        _tool_status = "ready"
        print("LanguageTool grammar checker is ON.")
    except ImportError:
        _tool_status = "failed"
    except Exception as e:
        _tool_status = "failed"
        print("LanguageTool couldn't start (is Java installed?):", e)


def start_grammar_checker():
    """Start LanguageTool in the background so the app opens instantly."""
    global _tool_status
    if _tool_status == "off":
        _tool_status = "loading"
        threading.Thread(target=_load_languagetool, daemon=True).start()


def grammar_hints(text: str) -> list:
    """Ask LanguageTool what looks wrong. Returns hints like: "me and him is" (maybe "me and him are")."""
    if _tool_status != "ready":
        return []
    hints = []
    # LanguageTool gets confused by lowercase texting, so check a capitalized copy (same length,
    # so the positions still match your original text). Your text itself isn't changed.
    proper = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    proper = re.sub(r"(?<![\w'])i(?=[\s'.,!?]|$)", "I", proper)
    try:
        for m in _tool.check(proper):
            if m.rule_id in IGNORE_RULES or m.category in IGNORE_CATEGORIES:
                continue
            bad = text[m.offset:m.offset + m.error_length]
            hint = f'"{bad}": {m.message}'
            if m.replacements:
                hint += f' (maybe "{m.replacements[0]}")'
            hints.append(hint)
    except Exception as e:
        print("Grammar check error:", e)
    return hints[:6]
