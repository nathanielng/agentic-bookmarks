"""Pass 1: the LLM reads a sample of the library and proposes a category list (PLAN03 §11.2).

The result is a 'taxonomy' changeset of add_category items for you to edit and approve before anything is
classified. Between MIN and MAX categories: enough to separate your interests, few enough to scan, and at
most 15 so that, with 'Other', the classifier reads every option in one pass.
"""

import json
import random
import re
from collections.abc import Callable

from .. import changesets
from ..service import Library
from . import AINotReady

PROMPT_VERSION = "taxonomy-v1"
MIN_CATEGORIES, MAX_CATEGORIES = 8, 15
SAMPLE = 400
RESERVED = {"other", "others", "misc", "miscellaneous", "general", "uncategorised", "uncategorized", "none"}

SYSTEM = (
    "You organise a person's saved web bookmarks into categories. You reply with JSON only, no prose."
)


def sample_lines(lib: Library, size: int = SAMPLE, seed: int = 7) -> tuple[list[str], int]:
    rows = lib.conn.execute(
        "SELECT title, domain, description FROM bookmarks WHERE deleted_at IS NULL AND merged_into_id IS NULL"
    ).fetchall()
    rows = [r for r in rows if r["title"] or r["description"]]
    total = len(rows)
    if total > size:
        rows = random.Random(seed).sample(rows, size)
    lines = []
    for r in rows:
        text = (r["title"] or r["description"] or "")[:100].replace("\n", " ")
        lines.append(f"- {text} ({r['domain']})")
    return lines, total


def build_messages(lines: list[str], total: int, existing: list[dict], lo: int, hi: int, feedback: str | None = None) -> list[dict]:
    keep = ""
    if existing:
        keep = "These categories already exist and stay; propose only additional ones, so that the total is within the range:\n" + "\n".join(
            f"- {c['name']}: {c['description'] or ''}" for c in existing
        ) + "\n\n"
    user = (
        f"Here are {len(lines)} of the {total} bookmarks (title and site):\n" + "\n".join(lines) + "\n\n" + keep +
        f"Propose between {lo} and {hi} categories in total that cover these bookmarks well.\n"
        "- Not too broad and not too fine: each category should hold a meaningful share (roughly 3% or more) and none "
        "should swallow more than about a fifth.\n"
        "- Names: 1 to 4 words, title case, distinct from each other.\n"
        "- Description: one sentence saying what belongs there, worded so a reader can tell it apart from the neighbours.\n"
        "- Do not propose a catch-all such as Other, Misc or General; that one is added automatically.\n"
        'Reply with exactly: {"categories": [{"name": "...", "description": "..."}]}'
    )  # fmt: skip
    if feedback:
        user += f"\n\nYour previous answer was rejected: {feedback}. Reply again with corrected JSON only."
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse(text: str, existing: list[dict], lo: int, hi: int) -> list[dict]:
    """The proposed new categories, or ValueError saying what is wrong (fed back to the model)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object found")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON ({exc.msg})") from exc
    cats = data.get("categories") if isinstance(data, dict) else None
    if not isinstance(cats, list):
        raise ValueError('expected {"categories": [...]}')
    seen = {c["name"].casefold() for c in existing}
    out = []
    for c in cats:
        if not isinstance(c, dict) or not str(c.get("name", "")).strip() or not str(c.get("description", "")).strip():
            raise ValueError("every category needs a name and a description")
        name = " ".join(str(c["name"]).split())[:40]
        if name.casefold() in RESERVED:
            raise ValueError(f"'{name}' is a catch-all; leave it out")
        if name.casefold() in seen:
            continue  # an existing category repeated back, or a duplicate
        seen.add(name.casefold())
        out.append({"name": name, "description": " ".join(str(c["description"]).split())[:300]})
    total = len(existing) + len(out)
    if not lo <= total <= hi:
        raise ValueError(f"{total} categories in total; it must be between {lo} and {hi}")
    return out


def mlx_generator(config) -> Callable[[list[dict]], str]:
    """Generate with the cached MLX model (loaded once per call of this function)."""
    try:
        from mlx_lm import generate, load
        from mlx_lm.sample_utils import make_sampler
    except ImportError as exc:
        raise AINotReady('the LLM needs mlx-lm: uv pip install -e ".[ai]"') from exc
    from .models import llm_path

    model, tokenizer = load(str(llm_path(config)))

    def run(messages: list[dict]) -> str:
        try:
            prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False, enable_thinking=False)
        except TypeError:
            prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        # Small prefill chunks: a ~9k-token prompt in 2048-token chunks runs out of GPU memory next to a 16 GB model on 24 GB Macs.
        return generate(model, tokenizer, prompt, max_tokens=1500, sampler=make_sampler(temp=0.3), prefill_step_size=256)

    return run


def propose(lib: Library, config, generator: Callable[[list[dict]], str] | None = None, lo: int = MIN_CATEGORIES,
            hi: int = MAX_CATEGORIES, author: str = "ai", on_progress: Callable[[str], None] | None = None,
            on_created: Callable[[int], None] | None = None) -> int:  # fmt: skip
    """Run pass 1 and store the proposal as a pending 'taxonomy' changeset; returns its id."""
    if not 2 <= lo <= hi <= 30:
        raise ValueError("the category range must be within 2–30")
    lines, total = sample_lines(lib)
    if total < 10:
        raise ValueError(f"only {total} bookmarks with a title; add more before proposing categories")
    existing = lib.categories()
    if len(existing) >= hi:
        raise ValueError(f"there are already {len(existing)} categories (the maximum is {hi})")
    say = on_progress or (lambda _msg: None)
    cs_id = changesets.create(lib, "taxonomy", author=author, provider="mlx", model=config.llm_model, prompt_version=PROMPT_VERSION,
                              summary=f"Proposing categories from {len(lines)} of {total} bookmarks", status="running",
                              params={"sample": len(lines), "total": total, "min": lo, "max": hi})  # fmt: skip
    if on_created:
        on_created(cs_id)
    try:
        say(f"loading {config.llm_model}")
        generate = generator or mlx_generator(config)
        feedback = None
        for attempt in range(3):
            say("asking the model" + (f" again ({feedback})" if feedback else ""))
            text = generate(build_messages(lines, total, existing, lo, hi, feedback))
            try:
                cats = parse(text, existing, lo, hi)
                break
            except ValueError as exc:
                feedback = str(exc)
        else:
            raise ValueError(f"the model's answer was rejected three times: {feedback}")
        for c in cats:
            changesets.add_item(lib, cs_id, "add_category", new_value=c)
        changesets.set_status(lib, cs_id, "pending", summary=f"{len(cats)} proposed categories from {len(lines)} of {total} bookmarks")
    except Exception as exc:
        changesets.set_status(lib, cs_id, "failed", error=str(exc))
        raise
    return cs_id
