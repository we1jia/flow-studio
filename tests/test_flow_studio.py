import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "flow_studio.py"
SPEC = importlib.util.spec_from_file_location("flow_studio", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

NODE = shutil.which("node")
FFMPEG = shutil.which("ffmpeg")

GUARD_CREATE = {"action": "create_agent_task_space", "reusable_task_space_ids": []}
GUARD_REUSE = {"action": "reuse_existing_task_space", "reusable_task_space_ids": [7]}


def run_helpers(expr: str):
    """在 node 里执行纯函数并返回 JSON 结果。"""
    code = MODULE.PURE_HELPERS_JS + f"\nconsole.log(JSON.stringify({expr}));\n"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip())


def base_cfg(mode: str) -> dict:
    return {
        "mode": mode,
        "prompt": "测试",
        "aspect": "16:9",
        "count": 1,
        "model": "Nano Banana 2.1",
        "thinking": None,
        "video_type": None,
        "resolution": None,
        "ref_images": [],
        "project_url": None,
        "project_name": "Agent CLI Studio",
        "out_dir": "/tmp/x",
        "file_prefix": "p",
        "max_credits": 0,
        "allow_unknown_cost": False,
        "dry_run": True,
        "timeout_sec": 60,
    }


@unittest.skipUnless(NODE, "需要 node")
class BlockingSignalTest(unittest.TestCase):
    def test_preexisting_failed_card_does_not_block(self):
        page = "Agent CLI Studio\n无法生成，请重试\n本网站受 reCAPTCHA 保护"
        result = run_helpers(f"detectNewBlockers({json.dumps(page)}, countKeywords({json.dumps(page)}), 'cat')")
        self.assertIsNone(result)

    def test_new_failure_after_submit_is_detected(self):
        before = "Agent CLI Studio\n无法生成，请重试\n本网站受 reCAPTCHA 保护"
        after = "无法生成，违反了我们的政策\n" + before
        result = run_helpers(f"detectNewBlockers({json.dumps(after)}, countKeywords({json.dumps(before)}), 'cat')")
        self.assertIn("无法生成", result["keywords"])
        self.assertIn("违反了我们的政策", result["keywords"])

    def test_captcha_interstitial_is_detected(self):
        before = "本网站受 reCAPTCHA 保护"
        after = "请完成验证码\nCAPTCHA\n本网站受 reCAPTCHA 保护"
        result = run_helpers(f"detectNewBlockers({json.dumps(after)}, countKeywords({json.dumps(before)}), 'cat')")
        self.assertIn("验证码", result["keywords"])

    def test_keyword_echoed_from_prompt_is_ignored(self):
        before = "Agent CLI Studio"
        after = "画一个写着请重试的按钮\nAgent CLI Studio"
        result = run_helpers(
            f"detectNewBlockers({json.dumps(after)}, countKeywords({json.dumps(before)}), '画一个写着请重试的按钮')"
        )
        self.assertIsNone(result)


@unittest.skipUnless(NODE, "需要 node")
class OriginalImageCandidateTest(unittest.TestCase):
    def test_prefers_original_then_full_size_then_preview(self):
        src = "https://flow.google.com/asb/ABC_def-123=s1600-rw"
        result = run_helpers(f"originalImageCandidates({json.dumps(src)})")
        self.assertEqual(
            [
                {"variant": "original", "url": "https://flow.google.com/asb/ABC_def-123=d"},
                {"variant": "full_size", "url": "https://flow.google.com/asb/ABC_def-123=s0"},
                {"variant": "preview", "url": src},
            ],
            result,
        )

    def test_never_falls_back_to_bare_thumbnail_url(self):
        src = "https://flow.google.com/asb/ABC=s1600-rw"
        urls = [c["url"] for c in run_helpers(f"originalImageCandidates({json.dumps(src)})")]
        self.assertNotIn("https://flow.google.com/asb/ABC", urls)

    def test_extension_mapping_rejects_non_image(self):
        result = run_helpers(
            "['image/jpeg', 'image/webp', 'image/png', 'text/html', ''].map(extForContentType)"
        )
        self.assertEqual([".jpg", ".webp", ".png", None, None], result)


@unittest.skipUnless(NODE, "需要 node")
class GeneratedScriptSyntaxTest(unittest.TestCase):
    def assert_js_syntax_ok(self, js: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gen.mjs"
            path.write_text(js, encoding="utf-8")
            proc = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(0, proc.returncode, proc.stderr)

    def test_image_and_video_scripts_parse(self):
        for guard in (GUARD_CREATE, GUARD_REUSE):
            self.assert_js_syntax_ok(MODULE.build_generate_js(guard, base_cfg("image")))
            self.assert_js_syntax_ok(MODULE.build_generate_js(guard, base_cfg("video")))

    def test_credits_script_parses(self):
        self.assert_js_syntax_ok(MODULE.build_credits_js(GUARD_CREATE))


IMAGE_RESULT = {
    "ok": True,
    "dry_run": False,
    "mode": "image",
    "project_url": "https://flow.google.com/project/x",
    "prompt": "很长的提示词" * 20,
    "credits_before": 9908,
    "estimated_credits": 0,
    "credits_after": 9908,
    "settings": {
        "mode": {"found": True, "selected": "image 图片"},
        "aspect": {"found": True, "selected": "crop_16_9 16:9"},
        "count": {"found": True, "selected": "x1"},
        "resolution": None,
        "model": "🍌 Nano Banana 2.1",
        "cost_text": "生成将消耗 0 个点数",
        "estimated_credits": 0,
    },
    "files": [
        {
            "path": "/out/a.jpg",
            "bytes": 1,
            "content_type": "image/jpeg",
            "variant": "original",
            "preview_width": 1376,
            "preview_height": 768,
            "source_url": "https://flow.google.com/asb/" + "x" * 240 + "=d",
            "width": 1376,
            "height": 768,
            "preview": "/out/_preview/a.jpg",
        }
    ],
    "window_guard_after": {"action": "create_agent_task_space", "single_window_enforced": True, "work_window_titles": ["W"]},
    "notices": [],
}


class CompactOutputTest(unittest.TestCase):
    def test_success_keeps_only_agent_fields(self):
        out = MODULE.compact_result(IMAGE_RESULT)
        self.assertEqual(
            {
                "ok": True,
                "model": "Nano Banana 2.1",
                "credits": {"before": 9908, "estimated": 0, "after": 9908},
                "files": [
                    {"path": "/out/a.jpg", "preview": "/out/_preview/a.jpg", "variant": "original", "width": 1376, "height": 768}
                ],
                "guard_after": "create_agent_task_space",
            },
            out,
        )
        full = json.dumps(IMAGE_RESULT, ensure_ascii=False, indent=2)
        small = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(small), len(full) / 3)

    def test_unapplied_setting_becomes_warning(self):
        result = json.loads(json.dumps(IMAGE_RESULT))
        result["settings"]["resolution"] = {"found": False, "groupHint": "视频分辨率", "optionText": "360p"}
        out = MODULE.compact_result(result)
        self.assertEqual(["resolution=360p 未生效（当前模型可能不支持）"], out["warnings"])

    def test_default_720p_on_model_without_resolution_is_not_warned(self):
        result = json.loads(json.dumps(IMAGE_RESULT))
        result["settings"]["resolution"] = {"found": False, "groupHint": "视频分辨率", "optionText": "720p"}
        self.assertNotIn("warnings", MODULE.compact_result(result))

    def test_error_message_is_truncated(self):
        out = MODULE.compact_result({"ok": False, "error": "runtime_error", "message": "x" * 5000})
        self.assertEqual("runtime_error", out["error"])
        self.assertLess(len(out["message"]), 600)

    def test_credits_output_hides_email(self):
        out = MODULE.compact_credits(
            {"ok": True, "tier": "ULTRA", "email": "someone@example.com", "credits": 9908,
             "reset_text": "重置时间：12月14日01:45", "visible_watermark": "false", "notices": []}
        )
        self.assertNotIn("email", out)
        self.assertEqual("12月14日01:45", out["reset"])


def fake_result(mode, estimated, before=1000, after=None, ok=True, error=None, name="x"):
    after = before - estimated if after is None else after
    res = {"ok": ok, "mode": mode, "credits_before": before, "estimated_credits": estimated, "credits_after": after,
           "settings": {"model": "M"}, "window_guard_after": {"action": "create_agent_task_space"}, "notices": []}
    if ok:
        res["files"] = [{"path": f"/out/{name}.mp4", "preview": f"/out/_preview/{name}_sheet.jpg"}]
    else:
        res["error"] = error
        res["message"] = "boom"
    return res


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_plan(self, plan):
        path = self.dir / "ep01.json"
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return path

    def run_batch(self, argv, side_effect):
        calls = []

        def fake_generate(params):
            calls.append(dict(params))
            return side_effect(params, len(calls))

        args = MODULE.build_parser().parse_args(["batch", *argv, "--out-dir", str(self.dir / "out")])
        buf = io.StringIO()
        with mock.patch.object(MODULE, "generate_once", side_effect=fake_generate), contextlib.redirect_stdout(buf):
            code = MODULE.cmd_batch(args)
        return code, json.loads(buf.getvalue()), calls

    def test_video_plan_requires_total_budget(self):
        plan = self.write_plan({"defaults": {"mode": "video"}, "shots": [{"id": "s01", "prompt": "a"}]})
        code, out, calls = self.run_batch(["--plan", str(plan)], lambda p, n: fake_result("video", 5))
        self.assertEqual(2, code)
        self.assertEqual("max_total_credits_required", out["error"])
        self.assertEqual([], calls)

    def test_defaults_merge_and_total_budget_clamps_each_shot(self):
        (self.dir / "ref.png").write_bytes(b"x")
        plan = self.write_plan({
            "defaults": {"mode": "video", "aspect": "9:16", "video": {"model": "Veo 3.1 - Lite", "max_credits": 30}},
            "shots": [
                {"id": "s01", "prompt": "a", "ref_image": "ref.png"},
                {"id": "s02", "prompt": "b"},
                {"id": "s03", "prompt": "c", "mode": "image"},
            ],
        })
        code, out, calls = self.run_batch(
            ["--plan", str(plan), "--max-total-credits", "40"],
            lambda p, n: fake_result(p["mode"], 0 if p["mode"] == "image" else 24, name=p["name"]),
        )
        self.assertEqual(0, code)
        self.assertEqual([30, 16, 0], [c["max_credits"] for c in calls])
        self.assertEqual("Veo 3.1 - Lite", calls[0]["model"])
        self.assertEqual([str((self.dir / "ref.png").resolve())], calls[0]["ref_image"])
        self.assertEqual("ep01_s02", calls[1]["name"])
        self.assertEqual("Nano Banana 2.1", calls[2]["model"])
        self.assertEqual(["9:16", "9:16", "9:16"], [c["aspect"] for c in calls])
        self.assertEqual({"ok": 3, "failed": 0, "skipped": 0, "pending": 0}, out["counts"])
        self.assertEqual(48, out["credits"]["spent"])
        self.assertTrue(Path(out["result_file"]).is_file())

    def test_stops_on_first_failure_and_marks_rest_pending(self):
        plan = self.write_plan({"defaults": {"mode": "video"}, "shots": [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]})
        code, out, calls = self.run_batch(
            ["--plan", str(plan), "--max-total-credits", "100"],
            lambda p, n: fake_result("video", 5) if n == 1 else fake_result("video", 5, ok=False, error="runtime_error"),
        )
        self.assertEqual(1, code)
        self.assertEqual(2, len(calls))
        self.assertEqual(["ok", "failed", "pending"], [s["status"] for s in out["shots"]])

    def test_skip_existing_resumes_without_regenerating(self):
        out_dir = self.dir / "out"
        out_dir.mkdir()
        (out_dir / "ep01_s01.mp4").write_bytes(b"x")
        plan = self.write_plan({"defaults": {"mode": "video"}, "shots": [{"id": "s01", "prompt": "a"}, {"id": "s02", "prompt": "b"}]})
        code, out, calls = self.run_batch(
            ["--plan", str(plan), "--max-total-credits", "100", "--skip-existing"],
            lambda p, n: fake_result("video", 5),
        )
        self.assertEqual(0, code)
        self.assertEqual(["ep01_s02"], [c["name"] for c in calls])
        self.assertEqual("skipped", out["shots"][0]["status"])

    def test_dry_run_continues_past_budget_exceeded(self):
        plan = self.write_plan({"defaults": {"mode": "video"}, "shots": [{"prompt": "a"}, {"prompt": "b"}]})
        code, out, calls = self.run_batch(
            ["--plan", str(plan), "--dry-run"],
            lambda p, n: fake_result("video", 120, ok=False, error="budget_exceeded") if n == 1 else fake_result("video", 5),
        )
        self.assertEqual(2, len(calls))
        self.assertTrue(all(c["dry_run"] for c in calls))
        self.assertEqual(125, out["credits"]["estimated_total"])

    def test_typo_in_plan_is_rejected(self):
        plan = self.write_plan({"defaults": {"mode": "video"}, "shots": [{"prompt": "a", "resoluton": "360p"}]})
        code, out, calls = self.run_batch(["--plan", str(plan), "--dry-run"], lambda p, n: fake_result("video", 5))
        self.assertEqual(2, code)
        self.assertEqual("invalid_plan", out["error"])
        self.assertIn("resoluton", out["message"])
        self.assertEqual([], calls)


@unittest.skipUnless(FFMPEG and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class PreviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_video(self, size, seconds=8):
        path = self.dir / f"clip_{size.replace('x', '_')}.mp4"
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={seconds}",
             "-pix_fmt", "yuv420p", str(path)],
            check=True,
        )
        return path

    def size_of(self, path):
        meta = MODULE.probe_video_meta(str(path))
        return meta["width"], meta["height"]

    def test_landscape_sheet_is_2x2_640px(self):
        clip = self.make_video("1280x720")
        result = {"files": [{"path": str(clip)}]}
        MODULE.postprocess_files(result, "video")
        item = result["files"][0]
        self.assertEqual((1280, 720), (item["width"], item["height"]))
        self.assertAlmostEqual(8.0, item["duration"], places=0)
        self.assertEqual((640, 360), self.size_of(item["preview"]))

    def test_portrait_sheet_is_4x1(self):
        clip = self.make_video("720x1280")
        result = {"files": [{"path": str(clip)}]}
        MODULE.postprocess_files(result, "video")
        w, h = self.size_of(result["files"][0]["preview"])
        self.assertEqual(360, h)
        self.assertLess(w, 900)

    def test_image_preview_long_edge_512(self):
        img = self.dir / "big.png"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1376x768", "-frames:v", "1", str(img)], check=True)
        result = {"files": [{"path": str(img)}]}
        MODULE.postprocess_files(result, "image")
        item = result["files"][0]
        self.assertEqual((1376, 768), (item["width"], item["height"]))
        w, h = self.size_of(item["preview"])
        self.assertEqual(512, max(w, h))


# ---------- 生图直连 ----------


def run_direct_helpers(expr: str):
    """在 node 里执行直连纯函数并返回 JSON 结果。"""
    code = MODULE.PURE_DIRECT_JS + f"\nconsole.log(JSON.stringify({expr}));\n"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip())


FIFE = "https://flow-content.google/image/176da64d-eae4-4fd2-b28f-8769bd0988fa?Expires=1795550673&KeyName=k&Signature=s"
# 结构取自 2026-11-24 真机回包（签名与 ID 已替换），payload 是嵌在字符串里的 JSON，URL 内的 = 被转义成 =
SUCCESS_PAYLOAD = json.dumps(
    [[["176da64d-eae4-4fd2-b28f-8769bd0988fa", None, "wf-1", None, None, None,
       [[None, 1626600630, None, None, None, None, 1, "a red fox", 36, None, None, "wf-1", None, FIFE, FIFE]]]]]
).replace("=", "\\u003d")
SUCCESS_REPLY = (
    ")]}'\n\n1294\n"
    + json.dumps([["wrb.fr", "ogiZ0b", SUCCESS_PAYLOAD, None, None, None, "generic"]])
    + "\n25\n"
    + json.dumps([["di", 2100], ["af.httprm", 2100, "-1", 7]])
    + "\n"
)
REFUSAL_REPLY = ")]}'\n\n200\n" + json.dumps(
    [["wrb.fr", "ogiZ0b", None, None, None,
      [7, None, [["type.googleapis.com/google.rpc.ErrorInfo", ["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]]]], "generic"]]
) + "\n"


@unittest.skipUnless(NODE, "需要 node")
class DirectHelpersTest(unittest.TestCase):
    def test_body_matches_gflow_wire_shape(self):
        out = run_direct_helpers(
            "(() => { const b = buildOgiz0bBody({prompt: 'a fox', model: 'BELUGA', projectId: 'P', token: 'TOK',"
            " at: 'AT', batchId: 'B', seeds: [11, 22], aspectWire: 3});"
            " const q = new URLSearchParams(b); const outer = JSON.parse(q.get('f.req'));"
            " return {at: q.get('at'), rpc: outer[0][0][0], tail: outer[0][0].slice(2), inner: JSON.parse(outer[0][0][1])}; })()"
        )
        self.assertEqual("AT", out["at"])
        self.assertEqual("ogiZ0b", out["rpc"])
        self.assertEqual([None, "generic"], out["tail"])
        inner = out["inner"]
        ctx = [None, 22, None, None, None, "P", None, None, None, None, ["TOK", 1]]
        self.assertEqual(ctx, inner[3])
        self.assertEqual(["B"], inner[4])
        self.assertEqual(2, len(inner[1]))  # 每个 seed 一张图
        entry = inner[1][0]
        self.assertEqual([11, 3, "BELUGA"], [entry[3], entry[4], entry[5]])
        self.assertEqual(ctx, entry[7])
        self.assertEqual([[["a fox"]]], entry[8])
        self.assertEqual(12, len(entry))

    def test_success_reply_yields_deduped_images(self):
        out = run_direct_helpers(f"parseOgiz0bReply({json.dumps(SUCCESS_REPLY)})")
        self.assertIsNone(out["refusal"])
        self.assertEqual(1, len(out["images"]))
        self.assertEqual("176da64d-eae4-4fd2-b28f-8769bd0988fa", out["images"][0]["media_id"])
        self.assertEqual(FIFE, out["images"][0]["url"])  # = 已还原为 =

    def test_unusual_activity_is_a_waf_refusal(self):
        out = run_direct_helpers(f"parseOgiz0bReply({json.dumps(REFUSAL_REPLY)})")
        self.assertEqual([], out["images"])
        self.assertEqual("waf", out["refusal"]["kind"])
        self.assertEqual(7, out["refusal"]["code"])

    def test_html_or_empty_body_is_neither_images_nor_refusal(self):
        for body in ("<html>login</html>", "", ")]}'\n\n12\n[[\"di\",1]]"):
            out = run_direct_helpers(f"parseOgiz0bReply({json.dumps(body)})")
            self.assertEqual({"images": [], "errors": [], "refusal": None}, out)


class DirectEligibilityTest(unittest.TestCase):
    def cfg(self, **kw):
        return {**base_cfg("image"), "dry_run": False, **kw}

    def test_default_text_to_image_is_eligible(self):
        self.assertIsNone(MODULE.direct_ineligible_reason(self.cfg()))
        self.assertIsNone(MODULE.direct_ineligible_reason(self.cfg(model="🍌 Nano Banana 2.1", aspect="9:16", count=4)))

    def test_unmeasured_combinations_go_to_ui(self):
        cases = {
            "mode_not_image": self.cfg(mode="video"),
            "dry_run": self.cfg(dry_run=True),
            "ref_image": self.cfg(ref_images=["/tmp/a.png"]),
            "thinking": self.cfg(thinking="高"),
            "model_unmeasured": self.cfg(model="Nano Banana Pro"),
            "aspect_unmeasured": self.cfg(aspect="21:9"),
            "count_out_of_range": self.cfg(count=5),
        }
        for reason, cfg in cases.items():
            self.assertEqual(reason, MODULE.direct_ineligible_reason(cfg), reason)


@unittest.skipUnless(NODE, "需要 node")
class DirectScriptSyntaxTest(unittest.TestCase):
    assert_js_syntax_ok = GeneratedScriptSyntaxTest.assert_js_syntax_ok

    def test_direct_script_parses(self):
        cfg = {**base_cfg("image"), "model_wire": "BELUGA", "aspect_wire": 3, "post_timeout_ms": 120000,
               "prompt": "含 __BOOTSTRAP__ 与 `反引号` 的提示词"}
        for guard in (GUARD_CREATE, GUARD_REUSE):
            js = MODULE.build_direct_image_js(guard, cfg)
            self.assertNotIn("__PURE_DIRECT_JS__", js)
            self.assertIn("含 __BOOTSTRAP__", js)  # 提示词里的占位符文本不会被展开
            self.assert_js_syntax_ok(js)


class DirectDownloadTest(unittest.TestCase):
    def fake_urlopen(self, content_type, data):
        resp = mock.MagicMock()
        resp.headers = {"Content-Type": content_type}
        resp.read.return_value = data
        resp.__enter__.return_value = resp
        return mock.patch.object(MODULE.urllib.request, "urlopen", return_value=resp)

    def test_saves_each_image_with_index_suffix(self):
        with tempfile.TemporaryDirectory() as tmp, self.fake_urlopen("image/jpeg", b"\xff\xd8jpeg"):
            files = MODULE.download_direct_images(
                [{"url": FIFE, "media_id": "m1"}, {"url": FIFE, "media_id": "m2"}], Path(tmp), "shot")
            self.assertEqual(["shot_1.jpg", "shot_2.jpg"], [Path(f["path"]).name for f in files])
            self.assertEqual("m2", files[1]["media_id"])
            self.assertFalse(list(Path(tmp).glob("*.part")))

    def test_non_image_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, self.fake_urlopen("text/html", b"<html>"):
            with self.assertRaises(RuntimeError):
                MODULE.download_direct_images([{"url": FIFE}], Path(tmp), "shot")


class GenerateRoutingTest(unittest.TestCase):
    """generate_once 的直连/回退路由：只 mock 浏览器与磁盘边界。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        patches = [
            mock.patch.object(MODULE, "check_guard_or_fail", return_value=GUARD_CREATE),
            mock.patch.object(MODULE.window_guard, "collect_state", return_value={"action": "create_agent_task_space"}),
            mock.patch.object(MODULE, "load_state", return_value={"default_project_url": "https://flow.google.com/project/x"}),
            mock.patch.object(MODULE, "save_state"),
            mock.patch.object(MODULE, "postprocess_files"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def params(self, **kw):
        return {**MODULE.BASE_PARAMS, **MODULE.MODE_DEFAULTS["image"], "mode": "image", "prompt": "a fox",
                "out_dir": self.tmp.name, **kw}

    def run_with(self, replies, **kw):
        def fake_node(js, timeout_sec=0):
            self.calls.append("direct" if "buildOgiz0bBody" in js else "ui")
            return replies[len(self.calls) - 1], []

        dl = mock.patch.object(MODULE, "download_direct_images", return_value=[{"path": "/tmp/p.jpg", "variant": "original"}])
        with mock.patch.object(MODULE, "run_ego_node", side_effect=fake_node), dl:
            return MODULE.generate_once(self.params(**kw))

    def test_eligible_image_goes_direct_only(self):
        result = self.run_with([{"ok": True, "transport": "direct", "images": [{"url": FIFE}]}])
        self.assertEqual(["direct"], self.calls)
        self.assertTrue(result["ok"])
        self.assertEqual("/tmp/p.jpg", result["files"][0]["path"])

    def test_pre_send_failure_falls_back_to_ui(self):
        result = self.run_with([
            {"ok": False, "fallback": True, "fallback_reason": "mint_failure:x"},
            {"ok": True, "files": []},
        ])
        self.assertEqual(["direct", "ui"], self.calls)
        self.assertEqual("ui", result["transport"])
        self.assertEqual("mint_failure:x", result["direct_fallback_reason"])

    def test_sent_request_failure_is_never_retried(self):
        result = self.run_with([{"ok": False, "transport": "direct", "error": "direct_post_unknown", "message": "m"}])
        self.assertEqual(["direct"], self.calls)
        self.assertEqual("direct_post_unknown", result["error"])

    def test_forced_direct_does_not_fall_back(self):
        result = self.run_with([{"ok": False, "fallback": True, "fallback_reason": "http_403"}], transport="direct")
        self.assertEqual(["direct"], self.calls)
        self.assertEqual("direct_unavailable", result["error"])

    def test_forced_direct_rejects_unmeasured_combination(self):
        result = self.run_with([], transport="direct", thinking="高")
        self.assertEqual([], self.calls)
        self.assertEqual("direct_unsupported", result["error"])

    def test_ui_transport_and_ineligible_combination_skip_direct(self):
        self.assertEqual("ui", self.run_with([{"ok": True}], transport="ui")["transport"])
        self.calls.clear()
        result = self.run_with([{"ok": True}], thinking="高")
        self.assertEqual(["ui"], self.calls)
        self.assertEqual("thinking", result["direct_skipped_reason"])

    def test_compact_output_reports_transport_and_fallback(self):
        out = MODULE.compact_result({"ok": True, "transport": "ui", "direct_fallback_reason": "http_403",
                                     "timings": {"total_ms": 18640}, "warning": "请求 2 张，只返回 1 张"})
        self.assertEqual("ui", out["transport"])
        self.assertEqual("http_403", out["direct_fallback"])
        self.assertEqual(18.6, out["seconds"])
        self.assertIn("请求 2 张，只返回 1 张", out["warnings"])


# ---------- 视频帧模式：首帧绑定、时长、生成后核对（2026-11-24 首帧事故回归） ----------


@unittest.skipUnless(FFMPEG, "需要 ffmpeg")
class FrameVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.img_a, self.img_b, self.video_a = str(d / "a.png"), str(d / "b.png"), str(d / "a.mp4")
        run = lambda *a: subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)
        run("-f", "lavfi", "-i", "testsrc2=s=640x360", "-frames:v", "1", self.img_a)
        run("-f", "lavfi", "-i", "cellauto=s=640x360", "-frames:v", "1", self.img_b)  # 与 testsrc2 的 SSIM 约 0.03
        run("-loop", "1", "-i", self.img_a, "-t", "1", "-r", "24", "-pix_fmt", "yuv420p", self.video_a)

    def tearDown(self):
        self.tmp.cleanup()

    def test_matching_first_frame_passes(self):
        result = {"ok": True, "files": [{"path": self.video_a}]}
        MODULE.verify_frames(result, [self.img_a])
        self.assertTrue(result["ok"])
        self.assertGreater(result["files"][0]["first_frame_ssim"], 0.9)

    def test_wrong_first_frame_fails_and_stops(self):
        result = {"ok": True, "files": [{"path": self.video_a}]}
        MODULE.verify_frames(result, [self.img_b])
        self.assertFalse(result["ok"])
        self.assertEqual("first_frame_mismatch", result["error"])
        self.assertLess(result["files"][0]["first_frame_ssim"], MODULE.FRAME_SSIM_MIN)


class UniqueUploadCopyTest(unittest.TestCase):
    def test_name_tracks_content(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(MODULE, "UPLOAD_CACHE_DIR", Path(tmp) / "cache"):
            a, b, a2 = Path(tmp) / "shot.png", Path(tmp) / "other" / "shot.png", Path(tmp) / "copy.png"
            b.parent.mkdir()
            a.write_bytes(b"AAA"); b.write_bytes(b"BBB"); a2.write_bytes(b"AAA")
            ca, cb = MODULE.unique_upload_copy(a), MODULE.unique_upload_copy(b)
            self.assertNotEqual(Path(ca).name, Path(cb).name)  # 同名不同图 → 不同上传名
            self.assertTrue(Path(ca).name.startswith("shot-") and ca.endswith(".png"))
            self.assertEqual(Path(MODULE.unique_upload_copy(a2)).name.split("-")[-1], Path(ca).name.split("-")[-1])


class VideoFrameModeRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ref = self.dir / "first.png"
        self.ref.write_bytes(b"PNG")
        for p in [
            mock.patch.object(MODULE, "check_guard_or_fail", return_value=GUARD_CREATE),
            mock.patch.object(MODULE.window_guard, "collect_state", return_value={"action": "create_agent_task_space"}),
            mock.patch.object(MODULE, "load_state", return_value={}),
            mock.patch.object(MODULE, "save_state"),
            mock.patch.object(MODULE, "postprocess_files"),
            mock.patch.object(MODULE, "UPLOAD_CACHE_DIR", self.dir / "cache"),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def params(self, **kw):
        return {**MODULE.BASE_PARAMS, **MODULE.MODE_DEFAULTS["video"], "mode": "video", "prompt": "p",
                "out_dir": str(self.dir), "video_type": "帧", "ref_image": [str(self.ref)], **kw}

    def test_more_than_two_frames_rejected_before_browser(self):
        with mock.patch.object(MODULE, "run_ego_node") as node:
            r = MODULE.generate_once(self.params(ref_image=[str(self.ref)] * 3))
        node.assert_not_called()
        self.assertEqual("invalid_ref_image", r["error"])

    def test_duration_and_hashed_ref_reach_browser_script(self):
        seen = {}
        def fake(js, timeout_sec=0):
            seen["js"] = js
            return {"ok": True, "files": [{"path": str(self.dir / "v.mp4")}]}, []
        with mock.patch.object(MODULE, "run_ego_node", side_effect=fake), \
             mock.patch.object(MODULE, "frame_similarity", return_value=0.88):
            r = MODULE.generate_once(self.params(duration=4))
        self.assertIn('"duration": 4', seen["js"])
        self.assertIn("bindVideoFrames", seen["js"])
        self.assertRegex(seen["js"], r"first-[0-9a-f]{8}\.png")
        self.assertTrue(r["ok"])
        self.assertEqual(0.88, r["files"][0]["first_frame_ssim"])

    def test_mismatch_after_generation_marks_failure(self):
        with mock.patch.object(MODULE, "run_ego_node", return_value=({"ok": True, "files": [{"path": str(self.dir / "v.mp4")}]}, [])), \
             mock.patch.object(MODULE, "frame_similarity", return_value=0.21):
            r = MODULE.generate_once(self.params())
        self.assertFalse(r["ok"])
        self.assertEqual("first_frame_mismatch", r["error"])

    def test_duration_mismatch_skips_frame_check(self):
        files = [{"path": str(self.dir / "v.mp4"), "duration": 15.04}]
        with mock.patch.object(MODULE, "run_ego_node", return_value=({"ok": True, "files": files}, [])), \
             mock.patch.object(MODULE, "frame_similarity") as sim:
            r = MODULE.generate_once(self.params(duration=4))
        sim.assert_not_called()
        self.assertFalse(r["ok"])
        self.assertEqual("result_mismatch", r["error"])

    def test_dry_run_is_not_frame_checked(self):
        with mock.patch.object(MODULE, "run_ego_node", return_value=({"ok": True, "dry_run": True, "frame_binding": [{"verified": True}]}, [])), \
             mock.patch.object(MODULE, "frame_similarity") as sim:
            r = MODULE.generate_once(self.params(dry_run=True))
        sim.assert_not_called()
        self.assertTrue(r["ok"])

    def test_duration_flag_and_unapplied_warning(self):
        args = MODULE.build_parser().parse_args(["video", "--prompt", "p", "--duration", "4"])
        self.assertEqual(4, args.duration)
        warnings = MODULE.settings_warnings({"duration": {"found": False, "optionText": "4 秒"}})
        self.assertTrue(any("duration=4 秒" in w for w in warnings))


@unittest.skipUnless(NODE, "需要 node")
class FrameModeScriptSyntaxTest(unittest.TestCase):
    assert_js_syntax_ok = GeneratedScriptSyntaxTest.assert_js_syntax_ok

    def test_frame_mode_script_parses(self):
        cfg = {**base_cfg("video"), "video_type": "帧", "duration": 4, "ref_images": ["/tmp/a-1234abcd.png"], "dry_run": False}
        for guard in (GUARD_CREATE, GUARD_REUSE):
            js = MODULE.build_generate_js(guard, cfg)
            self.assertIn("async function bindVideoFrames", js)
            self.assertIn("FRAME_BIND_FAILED", js)
            self.assert_js_syntax_ok(js)

    def test_pick_guards_present_and_dry_run_script_parses(self):
        cfg = {**base_cfg("video"), "video_type": "帧", "duration": 4, "ref_images": ["/tmp/a-1234abcd.png"]}
        real = MODULE.build_generate_js(GUARD_CREATE, {**cfg, "dry_run": False})
        for marker in ("RESULT_PICK_MISMATCH", "elementFromPoint", "readDetailHeading", "settledFresh", "pick_diag"):
            self.assertIn(marker, real)
        dry = MODULE.build_generate_js(GUARD_CREATE, {**cfg, "dry_run": True})
        self.assertIn("baseline_preview", dry)
        self.assert_js_syntax_ok(dry)


class ResultPickGuardTest(unittest.TestCase):
    """拿错旧视频的防护（2026-11-24 真跑把项目里的旧视频当成新成品下载：15 秒 720p，而请求是 4 秒 360p）。"""

    def test_duration_mismatch_marks_result_mismatch(self):
        r = {"ok": True, "files": [{"path": "/x/v.mp4", "duration": 15.04}]}
        MODULE.verify_duration(r, 4)
        self.assertFalse(r["ok"])
        self.assertEqual("result_mismatch", r["error"])
        self.assertIn("不要重跑", r["message"])

    def test_duration_within_tolerance_passes(self):
        r = {"ok": True, "files": [{"path": "/x/v.mp4", "duration": 4.032}]}
        MODULE.verify_duration(r, 4)
        self.assertTrue(r["ok"])

    def test_duration_unknown_is_noop(self):
        r = {"ok": True, "files": [{"path": "/x/v.mp4"}]}
        MODULE.verify_duration(r, 4)
        MODULE.verify_duration(r, None)
        self.assertTrue(r["ok"])

    def test_ssim_threshold_between_real_samples(self):
        # 真实样例：错帧最高 0.21；360p 正确首/尾帧最低 0.57
        self.assertLess(0.21, MODULE.FRAME_SSIM_MIN)
        self.assertLess(MODULE.FRAME_SSIM_MIN, 0.57)

    def test_compact_result_keeps_diagnostics(self):
        out = MODULE.compact_result({"ok": True, "frame_binding": [{"slot": "start", "verified": True}],
                                     "baseline_preview": {"count": 18}, "pick_diag": {"after_submit_s": 70}})
        for key in ("frame_binding", "baseline_preview", "pick_diag"):
            self.assertIn(key, out)

    def test_progress_text_followed_by_space_counts_as_generating(self):
        js = MODULE.build_generate_js(GUARD_CREATE, {**base_cfg("video"), "dry_run": False})
        line = next(l.strip() for l in js.splitlines() if "const countProgress" in l)
        script = line + "\nconsole.log(JSON.stringify(['24%\\n', '生成中 24% ', '100%', '无进度'].map(countProgress)));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
        self.assertEqual([1, 1, 0, 0], json.loads(out.strip().splitlines()[-1]))

    def _run_js_line(self, marker, expr):
        js = MODULE.build_generate_js(GUARD_CREATE, {**base_cfg("video"), "dry_run": False})
        line = next(l.strip() for l in js.splitlines() if marker in l)
        out = subprocess.run(["node", "-e", line + "\nconsole.log(JSON.stringify(" + expr + "));"],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out.strip().splitlines()[-1])

    def test_media_key_ignores_thumbnail_suffix(self):
        # 2026-11-25 真跑复现：基线里最新一张旧卡的缩略图带 “=mm,18,15” 后缀，稍后变回不带后缀，旧卡被当成新成品
        got = self._run_js_line("const mediaKey", "['https://f/asb/AbC_-1=mm,18,15', 'https://f/asb/AbC_-1', 'https://l/x/DeF=s1600-rw', '']"
                                ".map(mediaKey)")
        self.assertEqual(["https://f/asb/AbC_-1", "https://f/asb/AbC_-1", "https://l/x/DeF", ""], got)

    def test_grid_order_puts_new_top_left_card_first(self):
        # 同一行按 left 排序：新成品插在左上角，旧卡被挤到第 2 位后不能再被选中
        got = self._run_js_line("const gridOrder",
                                "gridOrder([{key:'whale',top:76,left:854,isVideo:true},{key:'pier',top:424,left:248,isVideo:true},"
                                "{key:'ref',top:10,left:10,isVideo:false},{key:'plush',top:77,left:248,isVideo:true}]).map(x => x.key)")
        self.assertEqual(["plush", "whale", "pier"], got)

    def test_pick_uses_media_key_and_top_rank(self):
        js = MODULE.build_generate_js(GUARD_CREATE, {**base_cfg("video"), "video_type": "帧", "ref_images": ["/tmp/a-1234abcd.png"], "dry_run": False})
        self.assertIn("baseline.map(x => x.key)", js)
        self.assertIn("topKeys", js)
        self.assertIn("baseline_first", js)
        self.assertNotIn("=== targetSrc", js)
        self.assertIn("label_seen_before", js)

    def test_reused_label_adds_warning_without_failing(self):
        # 首帧比对与时长核对分不出“同首尾帧、同时长”的旧视频，标题撞上生成前的卡片时至少要提醒
        r = {"ok": True, "warning": "v.mp4 末帧与尾帧图差异较大（SSIM=0.3）", "pick_diag": {"label_seen_before": True}}
        MODULE.flag_reused_label(r)
        self.assertTrue(r["ok"])
        self.assertIn("末帧", r["warning"])
        self.assertIn("旧视频", r["warning"])
        clean = {"ok": True, "pick_diag": {"label_seen_before": False}}
        MODULE.flag_reused_label(clean)
        self.assertNotIn("warning", clean)


class SpaceIsolationTest(unittest.TestCase):
    """另一个 Agent 的定时任务与 Flow 真跑撞车：守卫不带前缀时把对方的 Space 当成可复用空间。"""

    X_SPACE = {"createdBy": "agent", "id": 115, "name": "news-radar-publish", "ownership": "agent", "taskId": "news-radar-publish"}
    USER_SPACE = {"createdBy": "user", "id": 0, "name": "", "ownership": "user", "taskId": ""}

    def test_guard_called_with_flow_prefix(self):
        with mock.patch.object(MODULE.window_guard, "collect_state", return_value={"action": "create_agent_task_space"}) as cs:
            MODULE.check_guard_or_fail()
        cs.assert_called_once_with(space_prefix=MODULE.SPACE_PREFIX)

    def test_prefix_matches_own_space_names(self):
        for name in ("flow studio credits", "flow studio video", "flow studio image direct"):
            self.assertTrue(name.startswith(MODULE.SPACE_PREFIX))

    def test_other_agent_space_is_foreign_not_reusable(self):
        state = MODULE.window_guard.decide(1, [self.USER_SPACE, self.X_SPACE], ["Google Flow - O2T"], space_prefix=MODULE.SPACE_PREFIX)
        self.assertEqual("create_agent_task_space", state["action"])
        self.assertEqual([], state["reusable_task_space_ids"])
        self.assertEqual([115], [s["id"] for s in state["foreign_agent_spaces"]])


if __name__ == "__main__":
    unittest.main()
