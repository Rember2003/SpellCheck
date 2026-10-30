"""
TextCheck - catches typos and autocorrect fails before you hit send.
Runs 100% locally with an open-source model (Gemma) through Ollama.

Setup (PowerShell):
    pip install gradio requests
    ollama pull gemma3:4b
    python app.py
Then open http://127.0.0.1:7860 in your browser.
"""

import difflib
import json
import time
import re

import gradio as gr
import requests

import db  # db.py: saves history, learns your typos, your personal words
import grammar_db  # 52k common misspellings + LanguageTool grammar rules

grammar_db.start_grammar_checker()

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gemma3:4b"  # change to "gemma3:1b" if your laptop is slow
NUM_GPU = 0  # 0 = run on CPU (fixes GPU driver crashes). Delete this from options to use GPU.

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
            "options": {"temperature": 0.2, "num_gpu": NUM_GPU},
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    data = json.loads(content)
    changes = pre_changes + [str(c) for c in data.get("changes", []) if str(c).strip()]
    fixed = str(data.get("fixed", message)).strip() or message
    fixed = re.sub(r"^message:\s*", "", fixed, flags=re.I)  # in case the model copies our label
    fixed = apply_listed_changes(fixed, changes)
    fixed = grammar_db.keep_slang(message, fixed, changes)  # put u/ur/tmrw back if the AI expanded them
    fixed = fix_contractions(fixed, changes)
    fixed = db.fix_learned(fixed, changes)  # typos you've made before
    tone = str(data.get("tone_warning", "")).strip()
    try:
        db.log_check(original, fixed, changes, tone, source="website", seconds=time.time() - start)
    except Exception as e:
        print("Couldn't save to database:", e)
    return {"fixed": fixed, "changes": changes, "tone_warning": tone}


def diff_words(original: str, fixed: str):
    """Word-level diff for gr.HighlightedText: removed words in red, added in green."""
    a, b = original.split(), fixed.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op == "equal":
            out.append((" ".join(a[i1:i2]) + " ", None))
        if op in ("delete", "replace"):
            out.append((" ".join(a[i1:i2]) + " ", "removed"))
        if op in ("insert", "replace"):
            out.append((" ".join(b[j1:j2]) + " ", "added"))
    return out


def check(message: str):
    if not message.strip():
        return "", [], "Type a message first."
    try:
        result = ask_model(message)
    except requests.exceptions.ConnectionError:
        return "", [], "⚠️ Can't reach Ollama. Is it running? (open the Ollama app or run `ollama serve`)"
    except Exception as e:  # bad JSON from model, timeout, etc.
        return "", [], f"⚠️ Something went wrong: {e}. Try again."

    notes = []
    if result["fixed"] == message.strip() and not result["changes"]:
        notes.append("👻 No curses found, send it!")
    else:
        notes += [f"🦇 {c}" for c in result["changes"]]
    if result["tone_warning"]:
        notes.append(f"\n🎃 **Tone check:** {result['tone_warning']}")

    return result["fixed"], diff_words(message, result["fixed"]), "\n".join(notes)


EXAMPLES = [
    "u should of told me ur coming tmrw, its a long drive and i definately cant pick u up their",
    "ill duck you up at 6 for the haloween party dont be late",
    "your welcome lol, their going to love my costume, its better then last year",
    "fine whatever do what u want, i should of known u wouldnt care",
]

WITCH = r"""
                       /\
                      /  \           ,-.-.    *
                     / *  \         ( ^ ^ )
                    /      \          `-'-'  .
                   /________\          *
           _______/__======__\_______
          '--------------------------'
              ////  .--------.  \\\\
             ///   /          \   \\\
             //   |  (O)  (O)  |   \\
             //   |  //    //  |   \\
             //    \    w     /    \\
              \\    '--------'    //
                 _.-'   <>   '-._
                /   \ \    / /   \
               /     \ \  / /     \   __________________
              /       \ \/ /       \ |__________________|>>>
             /_________\  /_________\
               /\/\/\/\/\/\/\/\/\/\/\
"""

CSS = """
.gradio-container {
    background: radial-gradient(circle at top, #2a0f3d 0%, #12051c 55%, #050208 100%) !important;
}
#title h1 { color: #ff8c1a; text-shadow: 0 0 12px #ff6a00, 0 0 28px #8a2be2; font-size: 2.6em; text-align: center; }
#title p { text-align: center; color: #d9b8ff; }
#witch { text-align: center; }
#witch pre { display: inline-block; text-align: left; background: transparent !important; border: none !important; }
#witch pre, #witch code { color: #c77dff !important; font-size: 12px; line-height: 1.15; background: transparent !important;
    text-shadow: 0 0 6px #8a2be2; }
button.primary { background: linear-gradient(90deg, #ff6a00, #8a2be2) !important; border: none !important;
    box-shadow: 0 0 14px #ff6a0088; font-size: 1.1em !important; }
button.primary:hover { box-shadow: 0 0 24px #ff8c1a; }
.block { border-color: #5a2a82 !important; }
footer { display: none !important; }
"""

def stats_view():
    st = db.stats()
    summary = (
        f"### 📜 Your Spellbook\n"
        f"**{st['checks']}** messages checked · **{st['messages_fixed']}** needed fixing · "
        f"**{st['words_fixed']}** mistakes caught · **{st['tone_warnings']}** tone warnings · "
        f"**{st['learned']}** typos learned · avg **{st['avg_seconds']}s** per check"
    )
    return summary, db.top_mistakes(), db.recent_checks()


def words_view():
    words = sorted(db.my_words())
    return ", ".join(words) if words else "_No words yet. Add names or slang you use._"


def add_word(word):
    for w in re.split(r"[,\s]+", word):
        db.add_word(w)
    return "", words_view()


def remove_word(word):
    for w in re.split(r"[,\s]+", word):
        if w:
            db.remove_word(w)
    return "", words_view()


with gr.Blocks(title="TextCheck") as demo:
    gr.Markdown(
        "# 🎃 TextCheck: The Spell-Check Witch 🧙‍♀️\n"
        "🦇 She casts **spell-check** on your texts. Catches typos, grammar and autocorrect fails, and keeps your vibe. "
        "Runs locally on open-source **Gemma**, so your texts never leave your laptop. 🦇",
        elem_id="title",
    )
    with gr.Accordion("🧙‍♀️ Meet the witch", open=True):
        gr.Markdown("```\n" + WITCH + "\n```", elem_id="witch")
    with gr.Tabs():
        with gr.Tab("🔮 Cast Spell-Check"):
            with gr.Row():
                with gr.Column():
                    inp = gr.Textbox(label="📝 Your cursed message", lines=4, placeholder="type ur msg here... 👻")
                    btn = gr.Button("🔮 Cast Spell-Check ✨", variant="primary")
                    gr.Examples(EXAMPLES, inputs=inp, label="🎃 Try a cursed text")
                with gr.Column():
                    fixed = gr.Textbox(label="✨ Un-cursed message (copy this)", lines=4)
                    diff = gr.HighlightedText(
                        label="🦇 What the witch changed",
                        color_map={"removed": "#ff4d4d", "added": "#7CFC00"},
                        combine_adjacent=True,
                    )
                    notes = gr.Markdown()

        with gr.Tab("📜 My Spellbook (Stats)") as stats_tab:
            summary = gr.Markdown()
            gr.Markdown(f"**Your most common mistakes.** After {db.LEARN_AFTER} times, TextCheck fixes them instantly.")
            top = gr.Dataframe(headers=["you typed", "should be", "times"], interactive=False)
            gr.Markdown("**Recent checks** (from the website and the hotkey)")
            recent = gr.Dataframe(headers=["time", "from", "original", "fixed", "tone warning"],
                                  interactive=False, wrap=True)
            refresh = gr.Button("🔄 Refresh")

        with gr.Tab("🕯️ My Words") as words_tab:
            gr.Markdown("Names, slang, or words you **never** want flagged as mistakes (like your friends' names).")
            word_in = gr.Textbox(label="Word(s)", placeholder="jaylen, finna, bussin")
            with gr.Row():
                add_btn = gr.Button("➕ Add", variant="primary")
                rm_btn = gr.Button("➖ Remove")
            words_list = gr.Markdown()

    btn.click(check, inputs=inp, outputs=[fixed, diff, notes])
    inp.submit(check, inputs=inp, outputs=[fixed, diff, notes])
    stats_tab.select(stats_view, outputs=[summary, top, recent])
    refresh.click(stats_view, outputs=[summary, top, recent])
    words_tab.select(words_view, outputs=words_list)
    add_btn.click(add_word, inputs=word_in, outputs=[word_in, words_list])
    word_in.submit(add_word, inputs=word_in, outputs=[word_in, words_list])
    rm_btn.click(remove_word, inputs=word_in, outputs=[word_in, words_list])

if __name__ == "__main__":
    demo.launch(
        css=CSS,
        theme=gr.themes.Default(primary_hue="orange", secondary_hue="purple"),
        js="() => { document.body.classList.add('dark'); }",  # spooky dark mode always
    )
