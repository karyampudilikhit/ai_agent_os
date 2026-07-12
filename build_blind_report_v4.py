"""Round 4: same blind-report generator, pointed at
benchmark_result_v4.json. Multiagent side now includes the real
clarification stage + synthesis + critique/refine — the full product
experience — against the UNCHANGED single_call baseline from round 1
(raw "type once into ChatGPT," no clarification). Kept separate so
rounds 1-3 stay intact for reference."""

import html
import json
import re

with open("benchmark_result_v4.json") as f:
    DATA = json.load(f)


def render_markdownish(text):
    text = html.escape(text)
    lines = text.split("\n")
    out = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        heading_match = re.match(r"^(#{1,4})\s+(.*)", stripped)
        if heading_match:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = min(len(heading_match.group(1)) + 2, 5)
            out.append(f"<h{level}>{heading_match.group(2)}</h{level}>")
            continue
        bullet_match = re.match(r"^[-*]\s+(.*)", stripped)
        if bullet_match:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{bullet_match.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{stripped}</p>")
    if in_list:
        out.append("</ul>")

    joined = "\n".join(out)
    joined = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", joined)
    joined = re.sub(r"`(.+?)`", r"<code>\1</code>", joined)
    return joined


sections = []
fallback_ids = []
for r in DATA["results"]:
    pid = r["id"]
    if r.get("synthesis_fallback"):
        fallback_ids.append(pid)
    objective = html.escape(r["objective"])
    a_html = render_markdownish(r["response_a"])
    b_html = render_markdownish(r["response_b"])
    sections.append(f"""
  <section class="prompt-block" id="p{pid}">
    <div class="prompt-head">
      <span class="pid">PROMPT {pid:02d} / 10</span>
      <p class="objective">{objective}</p>
    </div>
    <div class="pair">
      <article class="response">
        <div class="response-head">
          <span class="letter">A</span>
          <div class="vote-group" data-prompt="{pid}">
            <label><input type="radio" name="vote{pid}" value="A"> A wins</label>
            <label><input type="radio" name="vote{pid}" value="tie"> Tie</label>
            <label><input type="radio" name="vote{pid}" value="B"> B wins</label>
          </div>
        </div>
        <div class="response-body">{a_html}</div>
      </article>
      <article class="response">
        <div class="response-head">
          <span class="letter">B</span>
        </div>
        <div class="response-body">{b_html}</div>
      </article>
    </div>
  </section>
""")

fallback_note = (
    f'<div class="instructions" style="border-color:var(--border-strong);margin-top:10px;">'
    f'<b>Heads up:</b> prompt(s) {", ".join(str(i) for i in fallback_ids)} had no synthesizable '
    f"output this round (pipeline produced nothing to combine) — one side will read as a placeholder.</div>"
    if fallback_ids else ""
)

HTML_TEMPLATE = """<title>Round 4: full product vs raw ChatGPT</title>
<style>
  :root {{
    --bg: #f4f3ee; --surface: #ffffff; --surface-2: #ebeae3;
    --text-primary: #1a1c18; --text-secondary: #52564d; --text-tertiary: #83867c;
    --border: rgba(26, 28, 24, 0.13); --border-strong: rgba(26, 28, 24, 0.26);
    --accent: #3c6659; --accent-soft: rgba(60, 102, 89, 0.1);
    --shadow: 0 1px 2px rgba(26,28,24,0.05), 0 8px 20px -14px rgba(26,28,24,0.18);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #12160f; --surface: #1b201a; --surface-2: #212620;
      --text-primary: #e9ece5; --text-secondary: #aab0a2; --text-tertiary: #7c8276;
      --border: rgba(233, 236, 229, 0.1); --border-strong: rgba(233, 236, 229, 0.22);
      --accent: #7fc4ac; --accent-soft: rgba(127, 196, 172, 0.12);
      --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 8px 20px -14px rgba(0,0,0,0.5);
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #12160f; --surface: #1b201a; --surface-2: #212620;
    --text-primary: #e9ece5; --text-secondary: #aab0a2; --text-tertiary: #7c8276;
    --border: rgba(233, 236, 229, 0.1); --border-strong: rgba(233, 236, 229, 0.22);
    --accent: #7fc4ac; --accent-soft: rgba(127, 196, 172, 0.12);
    --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 8px 20px -14px rgba(0,0,0,0.5);
  }}
  :root[data-theme="light"] {{
    --bg: #f4f3ee; --surface: #ffffff; --surface-2: #ebeae3;
    --text-primary: #1a1c18; --text-secondary: #52564d; --text-tertiary: #83867c;
    --border: rgba(26, 28, 24, 0.13); --border-strong: rgba(26, 28, 24, 0.26);
    --accent: #3c6659; --accent-soft: rgba(60, 102, 89, 0.1);
    --shadow: 0 1px 2px rgba(26,28,24,0.05), 0 8px 20px -14px rgba(26,28,24,0.18);
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text-primary);
    font-family: "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.6; -webkit-font-smoothing: antialiased;
  }}
  h1, h2, h3, h4 {{ font-family: Cambria, Georgia, serif; text-wrap: balance; margin: 0.6em 0 0.3em; }}
  h1 {{ margin-top: 0; }}
  code {{ font-family: Consolas, "SF Mono", ui-monospace, monospace; background: var(--surface-2); padding: 1px 5px; border-radius: 4px; font-size: 0.9em; }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 48px 24px 120px; }}
  header.top {{ padding-bottom: 28px; border-bottom: 1px solid var(--border); margin-bottom: 20px; }}
  .eyebrow {{ font-size: 12px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent); }}
  h1 {{ font-size: 28px; }}
  .lead {{ color: var(--text-secondary); font-size: 15px; max-width: 68ch; }}
  .instructions {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px 20px; margin-top: 18px; font-size: 13.5px; color: var(--text-secondary);
  }}
  .instructions b {{ color: var(--text-primary); }}
  nav.toc {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 20px 0 8px; }}
  nav.toc a {{
    font-family: Consolas, ui-monospace, monospace; font-size: 12px;
    color: var(--text-secondary); text-decoration: none;
    border: 1px solid var(--border); border-radius: 5px; padding: 3px 8px;
  }}
  nav.toc a:hover {{ border-color: var(--border-strong); color: var(--text-primary); }}
  .prompt-block {{ margin-top: 44px; padding-top: 28px; border-top: 1px solid var(--border); scroll-margin-top: 20px; }}
  .prompt-head {{ margin-bottom: 16px; }}
  .pid {{ font-family: Consolas, ui-monospace, monospace; font-size: 11.5px; font-weight: 700; letter-spacing: 0.06em; color: var(--text-tertiary); }}
  .objective {{ font-size: 18px; font-family: Cambria, Georgia, serif; margin: 6px 0 0; color: var(--text-primary); text-wrap: balance; }}
  .pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; }}
  @media (max-width: 860px) {{ .pair {{ grid-template-columns: 1fr; }} }}
  .response {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    box-shadow: var(--shadow); overflow: hidden;
  }}
  .response-head {{
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 10px 16px; border-bottom: 1px solid var(--border); background: var(--surface-2);
  }}
  .letter {{
    font-family: Consolas, ui-monospace, monospace; font-weight: 700; font-size: 13px;
    width: 24px; height: 24px; display: flex; align-items: center; justify-content: center;
    border-radius: 6px; border: 1px solid var(--border-strong); color: var(--text-primary);
  }}
  .vote-group {{ display: flex; gap: 10px; font-size: 12px; color: var(--text-secondary); }}
  .vote-group label {{ display: flex; align-items: center; gap: 4px; cursor: pointer; }}
  .response-body {{ padding: 16px 18px; font-size: 13.5px; color: var(--text-primary); max-height: 480px; overflow-y: auto; }}
  .response-body h3 {{ font-size: 15px; }}
  .response-body h4, .response-body h5 {{ font-size: 13.5px; }}
  .response-body p {{ margin: 0 0 8px; color: var(--text-secondary); }}
  .response-body ul {{ margin: 0 0 8px; padding-left: 20px; color: var(--text-secondary); }}
  .response-body li {{ margin-bottom: 3px; }}
  .response-body strong {{ color: var(--text-primary); }}
  .tally-bar {{
    position: sticky; bottom: 0; margin-top: 40px;
    background: var(--surface); border: 1px solid var(--border-strong); border-radius: 12px;
    padding: 16px 20px; box-shadow: var(--shadow); display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  }}
  .tally-bar span {{ font-size: 13px; color: var(--text-secondary); }}
  #tally-output {{
    flex: 1; min-width: 220px; font-family: Consolas, ui-monospace, monospace; font-size: 12.5px;
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 10px; color: var(--text-primary);
  }}
  button.copy-btn {{
    font-family: inherit; font-size: 13px; font-weight: 600; padding: 8px 14px;
    border-radius: 7px; border: 1px solid var(--border-strong); background: var(--accent-soft);
    color: var(--text-primary); cursor: pointer;
  }}
  button.copy-btn:hover {{ background: var(--surface-2); }}
</style>

<div class="wrap">
  <header class="top">
    <div class="eyebrow">Round 4 · clarification + synthesis + critique, full product · same 10 objectives</div>
    <h1>Full product vs. a single raw ChatGPT-style call</h1>
    <p class="lead">Same 10 objectives, same single-call baseline responses as round 1 — unchanged, no clarification, exactly what typing once into ChatGPT looks like. The multi-agent side now gets the FULL real product experience: clarifying questions first (simulated answers, same as the earlier standalone clarification test), then decompose, synthesize, and critique/refine. This is the actual product-vs-ChatGPT test, not an isolated ablation. Blind and randomized again — fresh coin flip per prompt.</p>
    <div class="instructions">
      <b>How to use this page:</b> same as before — read both, vote on which is more useful, copy your votes at the bottom when done.
    </div>
    {fallback_note}
    <nav class="toc">
      {toc_links}
    </nav>
  </header>

  {sections}

  <div class="tally-bar">
    <span><b>Your votes:</b></span>
    <input id="tally-output" type="text" readonly value="(vote on prompts above)">
    <button class="copy-btn" onclick="copyTally()">Copy votes</button>
  </div>
</div>

<script>
function updateTally() {{
  const out = [];
  for (let i = 1; i <= {n}; i++) {{
    const checked = document.querySelector(`input[name="vote${{i}}"]:checked`);
    out.push(checked ? `${{i}}:${{checked.value}}` : `${{i}}:?`);
  }}
  document.getElementById('tally-output').value = out.join(' ');
}}
function copyTally() {{
  const el = document.getElementById('tally-output');
  el.select();
  document.execCommand('copy');
}}
document.querySelectorAll('input[type=radio]').forEach(el => el.addEventListener('change', updateTally));
updateTally();
</script>
"""

toc_links = " ".join(f'<a href="#p{r["id"]}">{r["id"]:02d}</a>' for r in DATA["results"])

html_out = HTML_TEMPLATE.format(
    sections="\n".join(sections),
    toc_links=toc_links,
    fallback_note=fallback_note,
    n=len(DATA["results"]),
)

with open("blind_report_v4.html", "w", encoding="utf-8") as f:
    f.write(html_out)

print("Wrote blind_report_v4.html")
