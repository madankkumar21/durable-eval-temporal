from core.model import cost_usd, parse_letter


def test_parse_structured():
    assert parse_letter('{"answer": "B"}', 3) == "B"
    assert parse_letter('{"answer": "c"}', 3) == "C"


def test_parse_out_of_range_is_none():
    assert parse_letter('{"answer": "D"}', 3) is None
    assert parse_letter('{"answer": ""}', 3) is None


def test_parse_fallback_plain_text():
    assert parse_letter("B", 3) == "B"
    assert parse_letter("The answer is (C).", 3) == "C"
    assert parse_letter("Looking at the frames, I can track", 3) is None  # no bare single letter


def test_cost_haiku45():
    # $1 / $5 per MTok
    assert abs(cost_usd("claude-haiku-4-5-20251001", 3740, 9) - (3740e-6 * 1.0 + 9e-6 * 5.0)) < 1e-12
    assert cost_usd("stub", 100, 100) == 0.0


def test_parse_number_labels_map_to_letters():
    assert parse_letter('{"answer": "2"}', 3, "number") == "B"
    assert parse_letter('{"answer": "1"}', 3, "number") == "A"
    assert parse_letter('{"answer": "4"}', 3, "number") is None
    assert parse_letter('{"answer": "B"}', 3, "number") is None  # a letter is not a valid number label


def test_refusal_text_is_unparsed():
    assert parse_letter("refusal:general_harms", 3) is None


def test_request_kwargs_per_model():
    from core.model import MAX_TOKENS, _request_kwargs
    assert _request_kwargs("claude-haiku-4-5-20251001") == {}  # default sampling
    assert _request_kwargs("claude-sonnet-5-5") == {"thinking": {"type": "between_tools"}}
    # Opus 5.5: effort only, merged into output_config; no thinking params
    assert _request_kwargs("claude-opus-5-5") == {"output_config": {"effort": "medium"}}
    assert MAX_TOKENS["claude-opus-5-5"] >= 1024


def test_no_request_sends_sampling_params():
    # API version 2026-10-21 returns a 400 for temperature, top_p or top_k, on every model,
    # even when they arrive through extra_body.
    from core.model import PRICES, _request_kwargs
    for model_id in PRICES:
        kwargs = _request_kwargs(model_id)
        assert "extra_body" not in kwargs
        assert not {"temperature", "top_p", "top_k"} & kwargs.keys()


def test_opus_output_config_merges_effort_with_format(monkeypatch):
    import core.model as m

    sent = {}

    class Usage:
        input_tokens, output_tokens = 10, 5

    class Msg:
        stop_reason, usage = "end_turn", Usage()
        content = [type("B", (), {"type": "text", "text": '{"answer": "B"}'})()]

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                sent.update(kw)
                return Msg()

    monkeypatch.setattr(m, "_client", lambda: Client())
    text, _, _ = m._call_anthropic("claude-opus-5-5", [], 3, "letter")
    assert text == '{"answer": "B"}'
    assert sent["output_config"]["effort"] == "medium"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["max_tokens"] == m.MAX_TOKENS["claude-opus-5-5"]
    assert "thinking" not in sent and "extra_body" not in sent


def test_truncated_reply_is_a_parse_failure(monkeypatch):
    import core.model as m

    class Usage:
        input_tokens, output_tokens = 10, 4096

    class Msg:
        stop_reason, usage = "max_tokens", Usage()
        content = [type("B", (), {"type": "text", "text": '{"answer": "B'})()]

    class Client:
        class messages:
            create = staticmethod(lambda **kw: Msg())

    monkeypatch.setattr(m, "_client", lambda: Client())
    text, _, _ = m._call_anthropic("claude-opus-5-5", [], 3, "letter")
    assert m.parse_letter(text, 3) is None


def test_haiku45_rows_cached_at_temperature_0_are_not_reused(monkeypatch):
    import core.model as m
    from core.frames import FrameCfg
    from core.tasks import Item

    item = Item("t:v1", "t", "v1.mp4", "How many?", ("1", "2", "3"), "2", "v1")
    stub_hash = m.answer_item(item, "stub", FrameCfg(), "test").prompt_hash
    assert "-tdefault" not in stub_hash  # the stub's cache keys do not change

    monkeypatch.setattr(m, "_call_anthropic", lambda *a: ('{"answer": "B"}', 120, 9))
    for name in ("cache_get", "cache_put", "log_http_call"):
        monkeypatch.setattr(m, name, lambda *a: None)
    r = m.answer_item(item, "claude-haiku-4-5-20251001", FrameCfg(blind=True), "test")
    assert r.prompt_hash.endswith("-tdefault")


def test_blind_sends_no_frames_and_has_its_own_cache_key(monkeypatch):
    import core.model as m
    from core.frames import FrameCfg
    from core.tasks import Item

    def no_video(*a, **k):
        raise AssertionError("blind mode must not fetch or decode video")

    sent = {}

    def fake_call(model_id, content, n_options, label_style):
        sent["content"] = content
        return '{"answer": "B"}', 120, 9

    monkeypatch.setattr(m, "ensure_videos", no_video)
    monkeypatch.setattr(m, "sample_frames", no_video)
    monkeypatch.setattr(m, "_call_anthropic", fake_call)
    monkeypatch.setattr(m, "cache_get", lambda *a: None)
    monkeypatch.setattr(m, "cache_put", lambda r: None)
    monkeypatch.setattr(m, "log_http_call", lambda *a: None)
    item = Item("t:v9", "t", "v9.mp4", "How many?", ("1", "2", "3"), "2", "v9")
    r = m.answer_item(item, "claude-haiku-4-5-20251001", FrameCfg(blind=True), "test")
    assert r.pred_letter == "B"
    assert all(block["type"] == "text" for block in sent["content"])
    assert r.prompt_hash.endswith("-blind-tdefault")
    framed_hash = m.build_prompt(item, [], "letter")[1]
    assert r.prompt_hash != framed_hash


def test_permutation_zero_keeps_the_phase1_prompt_hash():
    from core.model import build_prompt
    from core.tasks import Item, permutation_order

    base = Item("t:v1", "t", "v1.mp4", "How many?", ("2", "3", "4"), "3", "v1")
    h0 = build_prompt(base, [b"frame"], "letter")[1]
    hashes = set()
    for k in range(6):
        order = permutation_order(k, 3)
        it = Item(base.item_id, "t", "v1.mp4", "How many?", tuple(base.candidates[j] for j in order), "3", "v1")
        hashes.add(build_prompt(it, [b"frame"], "letter")[1])
        if k == 0:
            assert build_prompt(it, [b"frame"], "letter")[1] == h0  # ordering 0 reuses the phase 1 cache
    assert len(hashes) == 6  # every other ordering is a new prompt
