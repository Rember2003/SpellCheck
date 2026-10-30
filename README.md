# 🎃 TextCheck: The Spell-Check Witch

**Type fast. Send right.** A private, open-source AI that fixes your typos, grammar, and autocorrect fails in **any app** with one hotkey, without killing your texting vibe.

Built at **Hacktoberfest 2026 Hack Day** (Lamar ACM, sponsored by Google & MLH) for the theme *Build with Open Source AI*.

---

## The problem
I text fast and don't reread. I send typos, missing apostrophes, and autocorrect fails like *"I'll duck you up at 6."* Normal grammar checkers make my texts sound like emails, and most of them send my private messages to the cloud.

## What it does
- ⌨️ **Works in any app** (Discord, Claude, Notepad, browser): select text, press **Ctrl+Shift+Space**, and the fixed text replaces it
- ✏️ Fixes **spelling, grammar, missing apostrophes, wrong words** (their/there), and **autocorrect fails** (duck → pick)
- 😎 **Keeps your style:** slang like `u`, `omw`, `tmrw`, `lol` stays
- 🔥 **Tone check:** warns you if a message sounds rude or passive-aggressive
- 🧠 **Learns your typos:** mistakes you make twice get fixed instantly next time
- 📊 **Web dashboard** with history, stats, and your top mistakes
- 🔒 **100% local:** your messages never leave your laptop

## How it works: 5 layers
| Layer | What it does |
|---|---|
| 1. Misspellings + grammar database | 52,000+ known misspellings and common grammar mistakes (`should of` → `should have`), fixed instantly |
| 2. LanguageTool | Open-source grammar rules that flag problems like `i has`, `he definitely coming` |
| 3. Spell checker | Flags unknown words but skips slang and your personal words |
| 4. **Gemma 3 (4B) via Ollama** | Open-source AI that reads the flags and fixes context mistakes, plus tone |
| 5. SQLite database | Saves history and learns your repeated typos |

## Setup (Windows)
1. Install [Ollama](https://ollama.com/download), then download the model:
   ```
   ollama pull gemma3:4b
   ```
2. Install the Python packages:
   ```
   pip install -r requirements.txt
   ```
3. Optional, for grammar rules: install Java with `winget install Microsoft.OpenJDK.21`
4. Start Ollama (CPU mode, if your GPU driver is old):
   ```
   $env:CUDA_VISIBLE_DEVICES="-1"; $env:OLLAMA_VULKAN="0"; ollama serve
   ```
5. In a new window, run the hotkey and/or the web app:
   ```
   python hotkey.py     # select text in any app, press Ctrl+Shift+Space
   python app.py        # web app at http://127.0.0.1:7860
   ```

## Files
| File | Purpose |
|---|---|
| `hotkey.py` | Global hotkey that fixes selected text in any app (Halloween witch included 🧙‍♀️) |
| `app.py` | Gradio web app: checker, stats, and personal words |
| `grammar_db.py` | Misspellings database, grammar rules, LanguageTool |
| `db.py` | SQLite history and typo learning |
| `common_misspellings.txt` | 52k misspellings (from [codespell](https://github.com/codespell-project/codespell), CC BY-SA 3.0) |
| `common_grammar.txt` | Common grammar mistakes. Add your own! |

## Challenges
- My GPU driver was too old for Ollama (CUDA and Vulkan crashes), so everything runs on the CPU
- The small model sometimes skipped mistakes or listed fixes it didn't apply. I fixed that with example fixes in the prompt, hints from the spell and grammar checkers, and code that applies any fix the model lists but forgets

## What's next
GPU support (1–2 second checks), a browser extension and phone keyboard, Spanish support, and fine-tuning Gemma on a grammar-correction dataset.

## Built with
Python · Ollama · Gemma 3 · LanguageTool · pyspellchecker · SQLite · Gradio
