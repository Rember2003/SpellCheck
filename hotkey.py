"""
TextCheck Hotkey - fix your text in ANY app (Discord, Claude, Word, browser...).
Uses Windows' built-in hotkey system (no 'keyboard' library needed).

How to use:
  1. Type your message in any text box.
  2. Select it (Ctrl+A).
  3. Press Ctrl+Shift+Space.  -> the fixed text replaces what you selected.

Setup (PowerShell, with `ollama serve` running in another window):
    pip install pyperclip requests
    python hotkey.py
Keep this window open. Press Ctrl+C here to quit.
"""

import ctypes
import json
import re
import threading
import time
from ctypes import wintypes

import pyperclip
import requests

import db  # db.py: saves history, learns your typos, your personal words
import grammar_db  # 52k common misspellings + LanguageTool grammar rules

grammar_db.start_grammar_checker()

MODEL = "gemma3:4b"    # "gemma3:1b" is faster
NUM_GPU = 0            # 0 = CPU only (avoids your GPU driver crash)
OLLAMA_URL = "http://localhost:11434/api/chat"

# Hotkey: Ctrl+Shift+Space
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0002, 0x0004, 0x4000
VK_SPACE, VK_SHIFT, VK_CONTROL = 0x20, 0x10, 0x11
VK_C, VK_V = 0x43, 0x56
WM_HOTKEY, PM_REMOVE, KEYEVENTF_KEYUP = 0x0312, 0x0001, 0x0002
HOTKEY_NAME = "Ctrl+Shift+Space"

user32 = ctypes.windll.user32

SYSTEM_PROMPT = """You are a typo and grammar checker for text messages. The user types fast and doesn't reread.
You are NOT chatting with them. Never reply to the message. Only return a corrected copy.
You also get words a spell checker flagged. They are usually wrong, but pick the word that
fits the sentence, because the spell checker's guess can be wrong.

Fix ALL of these:
1. Spelling mistakes ("promble" -> "problem", "anyting" -> "anything").
2. Missing apostrophes. These are mistakes, NOT style: dont->don't, cant->can't, wasnt->wasn't,
   im->i'm, ill->i'll, todays->today's, theres->there's, its->it's (when it means "it is").
3. Wrong words: their/there/they're, your/you're, to/too, then/than, were/we're.
4. Missing small words so the sentence makes sense ("there still problem" -> "there's still a problem").
5. Autocorrect fails: a word that makes no sense is a swap. Use the word that DOES make sense
   ("duck you up" -> "pick you up", "feed the god" -> "feed the dog", "meat you at 3" -> "meet you at 3").

Keep their style: slang and texting abbreviations (u, ur, lol, omw, idk, tmrw, gonna, wanna), lowercase, emojis.
Do NOT make it formal. Do NOT reword parts that are already correct. Do NOT add new ideas.

tone_warning: ONLY if the message genuinely sounds rude, angry, or passive-aggressive. Otherwise "".

First list every change, then write the fixed message with EVERY listed change applied.
Reply ONLY with JSON:
{"changes": ["\"old\" -> \"new\" (reason)"], "fixed": "corrected message", "tone_warning": ""}"""

# Example fixes, so the small model learns exactly what we want
FEW_SHOT = [
    ("omw to the stor do u need anyting",
     {"changes": ['"stor" -> "store" (spelling)', '"anyting" -> "anything" (spelling)'],
      "fixed": "omw to the store do u need anything", "tone_warning": ""}),
    ("dont forget to feed the god before u leave",
     {"changes": ['"dont" -> "don\'t" (apostrophe)', '"god" -> "dog" (autocorrect fail)'],
      "fixed": "don't forget to feed the dog before u leave", "tone_warning": ""}),
    ("cant come to todays practice my car still broke",
     {"changes": ['"cant" -> "can\'t" (apostrophe)', '"todays" -> "today\'s" (apostrophe)',
                  '"car still broke" -> "car is still broken" (missing word, grammar)'],
      "fixed": "can't come to today's practice my car is still broken", "tone_warning": ""}),
    ("lol ok see u tmrw",
     {"changes": [], "fixed": "lol ok see u tmrw", "tone_warning": ""}),
    ("ok cool i guess ur to busy for me as usual",
     {"changes": ['"to" -> "too" (wrong word)'],
      "fixed": "ok cool i guess ur too busy for me as usual",
      "tone_warning": "This might sound passive-aggressive."}),
]


def apply_listed_changes(fixed: str, changes: list) -> str:
    """Safety net: if the model listed a fix but forgot to apply it, apply it ourselves."""
    for c in changes:
        m = re.match(r'\s*["“]([^"”]+)["”]\s*->\s*["“]([^"”]+)["”]', c)
        if not m:
            continue
        old, new = m.group(1), m.group(2)
        if new not in fixed and re.search(r"(?<!\w)" + re.escape(old) + r"(?!\w)", fixed, re.I):
            fixed = re.sub(r"(?<!\w)" + re.escape(old) + r"(?!\w)", lambda _: new, fixed, count=1, flags=re.I)
    return fixed

# Common missing-apostrophe words. These are always fixed, no AI needed.
# (Words like "ill", "its", "lets", "were" are left to the AI since they can be real words.)
CONTRACTIONS = {
    "dont": "don't", "cant": "can't", "wont": "won't", "wasnt": "wasn't", "isnt": "isn't",
    "arent": "aren't", "werent": "weren't", "didnt": "didn't", "doesnt": "doesn't",
    "couldnt": "couldn't", "shouldnt": "shouldn't", "wouldnt": "wouldn't", "havent": "haven't",
    "hasnt": "hasn't", "hadnt": "hadn't", "im": "i'm", "ive": "i've", "youre": "you're",
    "theyre": "they're", "thats": "that's", "whats": "what's", "theres": "there's",
    "shes": "she's", "hes": "he's", "youve": "you've", "theyve": "they've", "wouldve": "would've",
    "couldve": "could've", "shouldve": "should've", "idk": "idk",
}


def fix_contractions(text: str, changes: list) -> str:
    def repl(m):
        word = m.group(0)
        fixed = CONTRACTIONS.get(word.lower())
        if not fixed or fixed == word.lower():
            return word
        if word[0].isupper():
            fixed = fixed[0].upper() + fixed[1:]
        changes.append(f'"{word}" -> "{fixed}" (apostrophe)')
        return fixed
    return re.sub(r"(?<![\w'])[A-Za-z]+(?![\w'])", repl, text)

# ---- Spell-check hints: a normal spell checker finds suspicious words, then Gemma decides the fix ----
try:
    from spellchecker import SpellChecker
    SPELL = SpellChecker()
except ImportError:
    SPELL = None
    print("Tip: run `pip install pyspellchecker` so TextCheck catches more mistakes.")

SLANG = set("""u ur ya yall lol lmao lmfao rofl omw idk idc tmrw tmr tn gonna wanna gotta kinda sorta
bruh fr ngl tbh imo imho btw rn pls plz thx ty tysm np nvm smh brb ttyl wyd hbu lmk ig istg ong
ok okay k kk ye yea yeah nah yep nope hmm omg wtf irl dm dms ikr jk asap ppl bc cuz cause tho
gf bf bff bestie lowkey highkey sus fyi aight yo sup hru wbu wym mb gg""".split())


def spell_hints(text: str) -> list:
    """Return hints like: "promble" (maybe "problem")."""
    if SPELL is None:
        return []
    hints, seen = [], set()
    personal = db.my_words()
    for i, m in enumerate(re.finditer(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)):
        word = m.group(0)
        low = word.lower()
        if (low in seen or "'" in word or len(low) < 2 or low in SLANG or low in personal or low in CONTRACTIONS
                or (word[0].isupper() and i > 0)):  # skip names in the middle of a sentence
            continue
        seen.add(low)
        if low in SPELL:
            continue
        if low.endswith("s") and low[:-1] in SPELL:          # todays -> today's
            guess = low[:-1] + "'s"
        else:
            guess = SPELL.correction(low)
        hints.append(f'"{word}"' + (f' (maybe "{guess}")' if guess and guess != low else ""))
    return hints


def make_user_content(message: str) -> str:
    hints = spell_hints(message)
    grammar = grammar_db.grammar_hints(message)
    content = "Message: " + message
    if hints:
        content += "\nSpell checker flagged: " + ", ".join(hints)
    if grammar:
        content += "\nGrammar checker flagged: " + "; ".join(grammar)
    if hints or grammar:
        content += "\n(Check each flagged item, and ALSO look for other mistakes the checkers can't see.)"
    return content

def build_messages(message: str) -> list:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for user_text, answer in FEW_SHOT:
        msgs.append({"role": "user", "content": make_user_content(user_text)})
        msgs.append({"role": "assistant", "content": json.dumps(answer)})
    msgs.append({"role": "user", "content": make_user_content(message)})
    return msgs

# ---------------- Halloween theme ----------------
import os
os.system("")  # turns on colors in Windows PowerShell

P="\033[95m"; G="\033[92m"; Y="\033[93m"; O="\033[38;5;208m"; W="\033[97m"; R="\033[0m"; K="\033[38;5;94m"; C="\033[96m"; B="\033[38;5;218m"
ART = rf"""
{P}                       /\
{P}                      /  \         {O}  ,-.-.    {Y}*
{P}                     / {Y}*{P}  \        {O} ( {Y}^ ^{O} )
{P}                    /      \        {O}  `-'-'  {Y}.
{P}                   /________\          {Y}*
{P}           _______/__{O}======{P}__\_______
{P}          '--------------------------'
{K}              ////{W}  .--------.  {K}\\\\
{K}             ///{W}   /          \   {K}\\\
{K}             //{W}   |  {G}(O)  (O){W}  |   {K}\\
{K}             //{W}   |  {B}//    //{W}  |   {K}\\
{K}             //{W}    \    {P}w{W}     /    {K}\\
{K}              \\{W}    '--------'    {K}//
{P}                 _.-'   {Y}<>{P}   '-._
{P}                /   {O}\ \    / /{P}   \
{P}               /     {O}\ \  / /{P}     \   {K}__________________
{P}              /       {O}\ \/ /{P}       \ {K}|__________________|{Y}>>>
{P}             /_________{O}\  /{P}_________\
{W}               /\/\/\/\/\/\/\/\/\/\/\{R}
"""


def show_banner():
    print(ART)
    print(f"{O}      🎃  {P}T E X T C H E C K{O}  🎃   {Y}the spell-check witch{R}")
    print(f"{C}      ✨ Select text in any app, press Ctrl+Shift+Space to cast spell-check ✨{R}")
    print(f"{W}      Keep this window open. Press Ctrl+C here to quit.{R}\n")


busy = threading.Lock()


def ask_model(message: str) -> dict:
    start = time.time()
    original = message
    pre_changes = []
    message = grammar_db.fix_misspellings(message, pre_changes, skip=db.my_words())  # instant fixes
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": build_messages(message),
            "format": "json",
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.2, "num_gpu": NUM_GPU},
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = json.loads(resp.json()["message"]["content"])
    changes = pre_changes + [str(c) for c in data.get("changes", []) if str(c).strip()]
    fixed = str(data.get("fixed", message)).strip() or message
    fixed = re.sub(r"^message:\s*", "", fixed, flags=re.I)  # in case the model copies our label
    fixed = apply_listed_changes(fixed, changes)
    fixed = fix_contractions(fixed, changes)
    fixed = db.fix_learned(fixed, changes)  # typos you've made before
    tone = str(data.get("tone_warning", "")).strip()
    try:
        db.log_check(original, fixed, changes, tone, source="hotkey", seconds=time.time() - start)
    except Exception as e:
        print("Couldn't save to database:", e)
    return {"fixed": fixed, "changes": changes, "tone_warning": tone}


def key_down(vk): user32.keybd_event(vk, 0, 0, 0)
def key_up(vk): user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def press_ctrl(vk):
    key_down(VK_CONTROL); key_down(vk); time.sleep(0.02)
    key_up(vk); key_up(VK_CONTROL)


def wait_for_keys_released():
    for _ in range(60):
        if not any(user32.GetAsyncKeyState(k) & 0x8000 for k in (VK_CONTROL, VK_SHIFT, VK_SPACE)):
            return
        time.sleep(0.05)


def popup(text: str):
    threading.Thread(
        target=user32.MessageBoxW,
        args=(0, text, "TextCheck 🎃", 0x30 | 0x40000),  # warning icon + always on top
        daemon=True,
    ).start()


def fix_selection():
    if not busy.acquire(blocking=False):
        print("Still working on the last one...")
        return
    try:
        wait_for_keys_released()
        old_clipboard = pyperclip.paste()
        pyperclip.copy("")
        press_ctrl(VK_C)
        time.sleep(0.3)
        text = pyperclip.paste()

        if not text.strip():
            print("Nothing selected. Select your text first (Ctrl+A), then press", HOTKEY_NAME)
            pyperclip.copy(old_clipboard)
            return

        print(f"\n{P}🔮 Casting spell-check on:{R} {text!r} ...")
        start = time.time()
        result = ask_model(text)

        if result["fixed"] != text.strip():
            pyperclip.copy(result["fixed"])
            press_ctrl(VK_V)
            print(f"{G}✨ Spell cast!{R} {W}{result['fixed']!r}{R}  ({time.time() - start:.1f}s)")
            for c in result["changes"]:
                print(f"   {Y}🦇{R}", c)
        else:
            print(f"{G}👻 No curses found, your text is clean!{R} ({time.time() - start:.1f}s)")

        if result["tone_warning"]:
            print(f"   {O}🎃 TONE CHECK:{R}", result["tone_warning"])
            popup("Tone check: " + result["tone_warning"])

        time.sleep(0.3)
        pyperclip.copy(old_clipboard)
    except requests.exceptions.ConnectionError:
        print("Can't reach Ollama. Is the `ollama serve` window still open?")
    except Exception as e:
        print("Something went wrong:", e)
    finally:
        busy.release()


def main():
    if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE):
        print(f"Couldn't register {HOTKEY_NAME}. Another app is already using it.")
        print("Close other copies of hotkey.py, or change the key in this file.")
        return
    show_banner()
    msg = wintypes.MSG()
    try:
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    print(f"{P}[{HOTKEY_NAME}] 🧹{R}")
                    threading.Thread(target=fix_selection, daemon=True).start()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print(f"{P}🦇 The witch flies away... bye!{R}")
    finally:
        user32.UnregisterHotKey(None, 1)


if __name__ == "__main__":
    main()
