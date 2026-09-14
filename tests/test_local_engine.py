from pathlib import Path
import sys
import zipfile

import pytest

from app.local_engine import (
    _extract_archive,
    apply_musetalk_windows_compat,
    bundled_bin_dir,
    inspect_engine,
    resolve_ffmpeg,
    tools_ffmpeg_dir,
)


def _make_engine(root: Path) -> None:
    (root / "models" / "musetalkV15").mkdir(parents=True)
    (root / "models" / "whisper").mkdir()
    (root / "scripts").mkdir()
    (root / "models" / "musetalkV15" / "unet.pth").write_bytes(b"weights")
    (root / "models" / "musetalkV15" / "musetalk.json").write_text("{}", encoding="utf-8")
    (root / "scripts" / "inference.py").write_text("# test", encoding="utf-8")


def test_inspect_engine_reports_required_files(tmp_path):
    engine = tmp_path / "engine"
    _make_engine(engine)
    result = inspect_engine(engine)
    assert result["installed"] is True
    assert result["missing"] == []


def test_extract_archive_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("../escape.txt", "no")
    with pytest.raises(ValueError, match="不安全路径"):
        _extract_archive(archive, tmp_path / "out")


def test_resolve_ffmpeg_prefers_data_tools_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    exe = tools_ffmpeg_dir(tmp_path) / "ffmpeg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    assert resolve_ffmpeg(tmp_path) == str(exe)


def test_resolve_ffmpeg_accepts_settings_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    bin_dir = tmp_path / "custom" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "ffmpeg.exe").write_bytes(b"stub")
    assert resolve_ffmpeg(tmp_path, {"FFMPEG_PATH": str(bin_dir)}) == str(bin_dir / "ffmpeg.exe")


def test_resolve_ffmpeg_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.setattr("app.local_engine.shutil.which", lambda name: r"C:\Fake\ffmpeg.exe" if name == "ffmpeg" else None)
    assert resolve_ffmpeg(tmp_path) == r"C:\Fake\ffmpeg.exe"


def test_resolve_ffmpeg_returns_empty_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.setattr("app.local_engine.shutil.which", lambda name: None)
    assert resolve_ffmpeg(tmp_path) == ""


def test_bundled_bin_dir_requires_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert bundled_bin_dir() is None
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "afan Talking Video Agent.exe"))
    assert bundled_bin_dir() == tmp_path / "_internal" / "bin"


def test_resolve_ffmpeg_uses_bundled_bin_when_frozen(tmp_path, monkeypatch):
    # 打包版全新安装：settings/env/tools 全部为空时，回落到安装包自带的 _internal/bin。
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    exe = tmp_path / "_internal" / "bin" / "ffmpeg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "afan Talking Video Agent.exe"))
    assert resolve_ffmpeg(tmp_path) == str(exe)


# 上游 MuseTalk inference.py 的骨架（含全部待补丁锚点），行尾用 CRLF 模拟真实引擎包。
_MUSE_FIXTURE = "\r\n".join([
    "def fast_check_ffmpeg():",
    "    try:",
    "        subprocess.run([\"ffmpeg\", \"-version\"], capture_output=True, check=True)",
    "        return True",
    "    except:",
    "        return False",
    "",
    "",
    "def main(args):",
    "    if not fast_check_ffmpeg():",
    "        if not fast_check_ffmpeg():",
    "            print(\"Warning: Unable to find ffmpeg, please ensure ffmpeg is properly installed\")",
    "",
    "    # Process each task",
    "    for task_id in inference_config:",
    "        try:",
    "            if get_file_type(video_path) == \"video\":",
    "                save_dir_full = os.path.join(temp_dir, input_basename)",
    "                os.makedirs(save_dir_full, exist_ok=True)",
    "                cmd = f\"ffmpeg -v fatal -i {video_path} -start_number 0 {save_dir_full}/%08d.png\"",
    "                os.system(cmd)",
    "                input_img_list = sorted(glob.glob(os.path.join(save_dir_full, '*.[jpJP][pnPN]*[gG]')))",
    "                fps = get_video_fps(video_path)",
    "            # Save prediction results",
    "            temp_vid_path = f\"{temp_dir}/temp_{input_basename}_{audio_basename}.mp4\"",
    "            cmd_img2video = f\"ffmpeg -y -v warning -r {fps} -f image2 -i {result_img_save_path}/%08d.png -vcodec libx264 -vf format=yuv420p -crf 18 {temp_vid_path}\"",
    "            print(\"Video generation command:\", cmd_img2video)",
    "            os.system(cmd_img2video)",
    "            cmd_combine_audio = f\"ffmpeg -y -v warning -i {audio_path} -i {temp_vid_path} {output_vid_name}\"",
    "            print(\"Audio combination command:\", cmd_combine_audio)",
    "            os.system(cmd_combine_audio)",
    "        except Exception as e:",
    "            traceback.print_exc()",
    "            print(\"Error occurred during processing:\", e)",
    "",
    "",
    "if __name__ == \"__main__\":",
    "    main(args)",
    "",
])


def test_apply_musetalk_windows_compat_patches_and_is_idempotent(tmp_path):
    target = tmp_path / "inference.py"
    target.write_text(_MUSE_FIXTURE, encoding="utf-8", newline="")
    assert apply_musetalk_windows_compat(target) == "patched"

    patched = target.read_text(encoding="utf-8")
    assert "os.system(cmd_img2video)" not in patched
    assert "os.system(cmd_combine_audio)" not in patched
    assert "subprocess.run([ffmpeg_bin" in patched
    assert "task_failed = True" in patched and "sys.exit(1)" in patched
    assert "def probe_fps(video_path, ffprobe_bin):" in patched
    compile(patched, "inference.py", "exec")

    backup = target.with_suffix(".py.afan-backup")
    assert backup.is_file()
    assert backup.read_bytes().decode("utf-8") == _MUSE_FIXTURE

    assert apply_musetalk_windows_compat(target) == "already"
    assert target.read_text(encoding="utf-8") == patched


def test_apply_musetalk_windows_compat_skips_unknown_content(tmp_path):
    target = tmp_path / "inference.py"
    target.write_text("print('already fixed upstream')\n", encoding="utf-8")
    assert apply_musetalk_windows_compat(target) == "skipped"
    assert target.read_text(encoding="utf-8") == "print('already fixed upstream')\n"

