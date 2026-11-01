// 「Web UI 上传入口合并」——前端纯策略层单测。
//
// 跑法:node --test openstoryline/web/tests/media_upload.test.mjs
// (pytest 里有一层包装,见 openstoryline/tests/test_media_upload_frontend.py)
//
// 被测的是 /static/media_upload.mjs —— 纯函数,不碰 DOM。
// 对应执行设计 §8.1 的 F05/F06/F07/F10 与 §6.1 握手载荷结构。
import test from "node:test";
import assert from "node:assert/strict";

import {
  AUDIO_EXTS,
  GENERIC_VIDEO_EXTS,
  IMAGE_EXTS,
  WORKFLOW_VIDEO_EXTS,
  buildImportVideoPayload,
  classifyMediaFile,
  fileExt,
  limitUploadBatch,
  planUploadBatch,
} from "../static/media_upload.mjs";

// -- 夹具 -------------------------------------------------------------------

// jsdom 没有 File 构造器的完整模拟也没关系:分类只读 f.type / f.name,
// 直接用普通对象即可。
const F = (name, type = "") => ({ name, type, size: 1 });

// ---------------------------------------------------------------------------
// fileExt
// ---------------------------------------------------------------------------

test("fileExt:大小写归一 / 无扩展名 / 末尾点", () => {
  assert.equal(fileExt("a.MP4"), ".mp4");
  assert.equal(fileExt("b.WebM"), ".webm");
  assert.equal(fileExt("noext"), "");
  assert.equal(fileExt("trailing."), "");
  assert.equal(fileExt(""), "");
  assert.equal(fileExt(null), "");
});

// ---------------------------------------------------------------------------
// classifyMediaFile —— 对应 §3.2 分类表
// ---------------------------------------------------------------------------

test("工作流白名单视频(.mp4/.mov/.mkv/.avi/.webm)→ workflow_video", () => {
  for (const ext of WORKFLOW_VIDEO_EXTS) {
    assert.equal(classifyMediaFile(F(`v${ext}`)), "workflow_video", ext);
    assert.equal(classifyMediaFile(F(`v${ext}`, "video/mp4")), "workflow_video", ext);
  }
});

test("图片:MIME 或扩展名 → image;tiff 等旧前端扩展名保留", () => {
  for (const ext of IMAGE_EXTS) {
    assert.equal(classifyMediaFile(F(`p${ext}`)), "image", ext);
  }
  assert.equal(classifyMediaFile(F("blob", "image/png")), "image");
  assert.equal(classifyMediaFile(F("camera", "image/jpeg")), "image");
});

test("旧版通用视频(.m4v/.flv/.wmv)只走普通素材,不进工作流(F06)", () => {
  for (const ext of [".m4v", ".flv", ".wmv"]) {
    assert.equal(classifyMediaFile(F(`v${ext}`)), "generic_video", ext);
  }
  // video/* MIME 但扩展名不在白名单:同样不进工作流
  assert.equal(classifyMediaFile(F("odd.bin", "video/x-matroska3d")), "generic_video");
});

test("音频先于视频/图片判定,拒绝上传(F05)", () => {
  for (const ext of AUDIO_EXTS) {
    assert.equal(classifyMediaFile(F(`a${ext}`)), "audio", ext);
  }
  assert.equal(classifyMediaFile(F("song", "audio/mpeg")), "audio");
  // 异常 MIME:音频扩展名优先于 image/* MIME
  assert.equal(classifyMediaFile(F("a.mp3", "image/png")), "audio");
});

test("无 MIME 但扩展名有效:按扩展名兜底(F07)", () => {
  assert.equal(classifyMediaFile(F("clip.mp4", "")), "workflow_video");
  assert.equal(classifyMediaFile(F("pic.png", "")), "image");
  assert.equal(classifyMediaFile(F("old.flv", "")), "generic_video");
});

test("不支持的类型 → unsupported(F08)", () => {
  assert.equal(classifyMediaFile(F("doc.txt")), "unsupported");
  assert.equal(classifyMediaFile(F("doc.pdf", "application/pdf")), "unsupported");
  assert.equal(classifyMediaFile(null), "unsupported");
  assert.equal(classifyMediaFile(F("", "")), "unsupported");
});

// ---------------------------------------------------------------------------
// planUploadBatch —— 顺序保持 + 拒收计数
// ---------------------------------------------------------------------------

test("混合批次:保持用户选择顺序,只含可上传条目(F04/F21)", () => {
  const mp4 = F("a.mp4");
  const png = F("b.png");
  const mp3 = F("c.mp3");
  const gif = F("d.gif");
  const flv = F("e.flv");
  const txt = F("f.txt");

  const plan = planUploadBatch([mp4, png, mp3, gif, flv, txt]);
  assert.deepEqual(
    plan.uploadList.map((it) => it.file.name),
    ["a.mp4", "b.png", "d.gif", "e.flv"],
  );
  assert.equal(plan.uploadList[0].isWorkflowVideo, true);
  assert.equal(plan.uploadList[1].isWorkflowVideo, false);
  assert.equal(plan.uploadList[2].isWorkflowVideo, false);
  assert.equal(plan.uploadList[3].isWorkflowVideo, false);
  assert.equal(plan.rejectedAudio, 1);
  assert.equal(plan.rejectedUnsupported, 1);
});

test("全不支持:uploadList 为空,拒收计数正确(F08)", () => {
  const plan = planUploadBatch([F("a.mp3"), F("b.txt")]);
  assert.equal(plan.uploadList.length, 0);
  assert.equal(plan.rejectedAudio, 1);
  assert.equal(plan.rejectedUnsupported, 1);
});

test("空输入不抛错", () => {
  for (const input of [null, undefined, []]) {
    const plan = planUploadBatch(input);
    assert.equal(plan.uploadList.length, 0);
    assert.equal(plan.rejectedAudio, 0);
    assert.equal(plan.rejectedUnsupported, 0);
  }
});

// ---------------------------------------------------------------------------
// limitUploadBatch —— F10 回归:旧实现对 const candidates 重新赋值抛 TypeError
// ---------------------------------------------------------------------------

test("F10:所选数量超过 pending 剩余容量 → 截断到 remain,保持顺序,不抛错", () => {
  const batch = [
    { file: F("1.mp4"), isWorkflowVideo: true },
    { file: F("2.png"), isWorkflowVideo: false },
    { file: F("3.mov"), isWorkflowVideo: true },
    { file: F("4.gif"), isWorkflowVideo: false },
  ];
  const { batch: limited, truncated } = limitUploadBatch(batch, 2);
  assert.equal(truncated, true);
  assert.deepEqual(limited.map((it) => it.file.name), ["1.mp4", "2.png"]);
});

test("F09 边界:remain=0 → 空批次;容量充足 → 原样返回不截断", () => {
  const batch = [{ file: F("1.mp4"), isWorkflowVideo: true }];
  assert.deepEqual(limitUploadBatch(batch, 0), { batch: [], truncated: true });
  const ok = limitUploadBatch(batch, 5);
  assert.equal(ok.truncated, false);
  assert.equal(ok.batch.length, 1);
  // 非法输入兜底
  assert.deepEqual(limitUploadBatch(null, 3), { batch: [], truncated: false });
});

// ---------------------------------------------------------------------------
// buildImportVideoPayload —— §6.1 兼容结构
// ---------------------------------------------------------------------------

test("载荷:files 数组 + 单值字段(= 第一个文件),stored_path 允许为空", () => {
  const payload = buildImportVideoPayload("sess-1", [
    { media: { id: "m1", name: "a.mp4" }, file: F("a.mp4") },
    { media: { id: "m2", name: "b.mov" }, file: F("b.mov") },
  ]);
  assert.equal(payload.session_id, "sess-1");
  assert.deepEqual(payload.files, [
    { media_id: "m1", filename: "a.mp4", stored_path: "" },
    { media_id: "m2", filename: "b.mov", stored_path: "" },
  ]);
  assert.equal(payload.media_id, "m1");
  assert.equal(payload.filename, "a.mp4");
  assert.equal(payload.stored_path, "");
});

test("载荷字段兜底:media.name > media.filename > file.name;path > stored_path", () => {
  const payload = buildImportVideoPayload("s", [
    { media: { id: "m", filename: "fb1", stored_path: "/x/y" }, file: F("fb2.mp4") },
  ]);
  assert.equal(payload.files[0].filename, "fb1");
  assert.equal(payload.files[0].stored_path, "/x/y");

  const payload2 = buildImportVideoPayload("s", [
    { media: { id: "" }, file: F("only-file.mp4") },
  ]);
  assert.equal(payload2.files[0].media_id, "");
  assert.equal(payload2.files[0].filename, "only-file.mp4");
});

test("空握手集合 → 结构完整但全空(不应被调用,防御性)", () => {
  const payload = buildImportVideoPayload("s", []);
  assert.deepEqual(payload, {
    session_id: "s",
    files: [],
    media_id: "",
    filename: "",
    stored_path: "",
  });
});

// ---------------------------------------------------------------------------
// 白名单一致性:前端工作流白名单必须与 node_04a 的 ACCEPT_EXTS 对齐
// ---------------------------------------------------------------------------

test("WORKFLOW_VIDEO_EXTS 与后端 ACCEPT_EXTS 完全一致", async () => {
  const { readFileSync } = await import("node:fs");
  const { fileURLToPath } = await import("node:url");
  const path = await import("node:path");
  const here = path.dirname(fileURLToPath(import.meta.url));
  const nodeSrc = readFileSync(
    // 2026-11-01 扁平化:web/ 上移仓库根,回溯两级即仓库根(原 openstoryline/web/ 需三级)
    path.join(here, "..", "..", "nodes", "node_04a_import_video.py"),
    "utf-8",
  );
  const m = nodeSrc.match(/ACCEPT_EXTS[^=]*=\s*\(([^)]*)\)/);
  assert.ok(m, "后端 ACCEPT_EXTS 定义未找到");
  const backendExts = [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]).sort();
  assert.deepEqual([...WORKFLOW_VIDEO_EXTS].sort(), backendExts);
});
