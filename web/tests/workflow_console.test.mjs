// /web/tests/workflow_console.test.mjs
//
// 工作流创建台纯逻辑层(/static/workflow_console.mjs)的 Node 单测
// (第二期阶段 3;运行方式与 workflow_monitor.test.mjs 同:node --test,
// 必须用 glob 形式 `node --test web/tests/*.test.mjs`)。
//
// 覆盖:文件预校验/去重/移除、参数校验、提交状态机、FormData 字段契约、
// 错误码 → 稳定提示、格式化杂项,以及 monitor reducer re-export 连通性。

import test from "node:test";
import assert from "node:assert/strict";

import {
  CONSOLE_API_PATHS, CONSOLE_BACKENDS, DEFAULT_LIMITS, HISTORY_STATE_LABEL, acceptCreated, addFiles,
  applyEvent, beginUpload, buildCreateFormData, canCancel, canDelete, canResume, canSubmit,
  createConsoleState, createMonitorState, failSubmission, filterHistoryRows,
  formatBytes, formatDuration, friendlyError, historyActions, makeFileEntry,
  parseResumeValue, removeFile, updateProgress, validateNewFiles, validateParams,
  hasErrors,
} from "../static/workflow_console.mjs";

function fakeFile(name, size, lastModified = 111) {
  return { name, size, lastModified, type: "application/octet-stream" };
}

const SMALL_LIMITS = {
  max_videos: 3,
  max_video_bytes: 100,
  prompt_max_chars: 20,
  target_duration_ms_default: 35000,
  target_duration_ms_max: 600000,
  allowed_video_exts: [".mp4", ".mov"],
  ratios: ["9:16", "16:9", "4:3"],
  langs: ["zh", "en"],
};

test("makeFileEntry 提取扩展名(大小写归一)", () => {
  assert.equal(makeFileEntry(fakeFile("a.MP4", 1)).ext, ".mp4");
  assert.equal(makeFileEntry(fakeFile("b.Mov", 1)).ext, ".mov");
  assert.equal(makeFileEntry(fakeFile("noext", 1)).ext, "");
});

test("validateNewFiles 放行合法文件并累积错误", () => {
  const { accepted, errors } = validateNewFiles(
    [fakeFile("a.mp4", 10), fakeFile("b.txt", 10), fakeFile("c.mov", 0),
     fakeFile("d.mp4", 999)],
    0, SMALL_LIMITS,
  );
  assert.equal(accepted.length, 1);
  assert.equal(accepted[0].name, "a.mp4");
  assert.equal(errors.length, 3);
  assert.match(errors.join("\n"), /格式不支持/);
  assert.match(errors.join("\n"), /空文件/);
  assert.match(errors.join("\n"), /大小上限/);
});

test("validateNewFiles 尊重数量上限(含已有文件数)", () => {
  const { accepted, errors } = validateNewFiles(
    [fakeFile("a.mp4", 1), fakeFile("b.mp4", 1), fakeFile("c.mp4", 1)],
    2, SMALL_LIMITS,
  );
  assert.equal(accepted.length, 1); // 只剩 1 个空位
  assert.equal(errors.length, 2); // 后两个文件各报一条
  for (const msg of errors) assert.match(msg, /数量超过上限/);
});

test("addFiles 按 name+size+lastModified 去重", () => {
  const state = createConsoleState();
  assert.equal(addFiles(state, [makeFileEntry(fakeFile("a.mp4", 5))]), 1);
  assert.equal(addFiles(state, [makeFileEntry(fakeFile("a.mp4", 5))]), 0);
  assert.equal(addFiles(state, [makeFileEntry(fakeFile("a.mp4", 6))]), 1);
  assert.equal(state.files.length, 2);
});

test("removeFile 边界安全", () => {
  const state = createConsoleState();
  addFiles(state, [makeFileEntry(fakeFile("a.mp4", 1))]);
  assert.equal(removeFile(state, 5), false);
  assert.equal(removeFile(state, 0), true);
  assert.equal(removeFile(state, 0), false);
  assert.equal(state.files.length, 0);
});

test("validateParams:空提示词/超长/非法时长/非法比例与语言", () => {
  const base = { prompt: "p", target_duration_ms: 35000, ratio: "9:16", lang: "zh" };
  assert.equal(hasErrors(validateParams(base, SMALL_LIMITS)), false);

  assert.match(validateParams({ ...base, prompt: "  " }, SMALL_LIMITS).prompt, /提示词/);
  assert.match(validateParams({ ...base, prompt: "字".repeat(21) }, SMALL_LIMITS).prompt, /过长/);

  for (const dur of [0, -5, 600001, 35.5]) {
    assert.match(
      validateParams({ ...base, target_duration_ms: dur }, SMALL_LIMITS).duration,
      /目标时长/,
    );
  }
  assert.match(validateParams({ ...base, ratio: "1:1" }, SMALL_LIMITS).ratio, /比例/);
  assert.match(validateParams({ ...base, lang: "fr" }, SMALL_LIMITS).lang, /语言/);
});

test("canSubmit:仅空闲态 + 有视频 + 参数合法", () => {
  const params = { prompt: "p", target_duration_ms: 35000, ratio: "9:16", lang: "zh" };
  const state = createConsoleState();
  assert.equal(canSubmit(state, params, SMALL_LIMITS), false); // 无视频
  addFiles(state, [makeFileEntry(fakeFile("a.mp4", 1))]);
  assert.equal(canSubmit(state, params, SMALL_LIMITS), true);
  beginUpload(state, 1);
  assert.equal(canSubmit(state, params, SMALL_LIMITS), false); // 上传中
});

test("提交状态机:beginUpload → updateProgress → acceptCreated / failSubmission", () => {
  const state = createConsoleState();
  addFiles(state, [makeFileEntry(fakeFile("a.mp4", 1))]);

  beginUpload(state, 100);
  assert.equal(state.phase, "uploading");
  updateProgress(state, 40, 100);
  assert.deepEqual(state.progress, { loaded: 40, total: 100 });
  updateProgress(state, 90, 100); // 非 uploading 态才忽略,这里仍在 uploading
  assert.equal(state.progress.loaded, 90);

  acceptCreated(state, { ok: true, thread_id: "console-abcdef123456", state: "starting" });
  assert.equal(state.phase, "live");
  assert.equal(state.threadId, "console-abcdef123456");
  assert.equal(state.error, null);

  const failed = createConsoleState();
  addFiles(failed, [makeFileEntry(fakeFile("a.mp4", 1))]);
  failSubmission(failed, { code: 503, message: "PostgreSQL 未就绪", detail: "" });
  assert.equal(failed.phase, "idle");
  assert.equal(failed.error.code, 503);
});

test("updateProgress 在非 uploading 态不生效", () => {
  const state = createConsoleState();
  updateProgress(state, 10, 100);
  assert.deepEqual(state.progress, { loaded: 0, total: 0 });
});

test("buildCreateFormData 字段与冻结契约一致", () => {
  const state = createConsoleState();
  addFiles(state, [
    makeFileEntry(new File([new Uint8Array([1, 2, 3])], "a.mp4", { lastModified: 1 })),
    makeFileEntry(new File([new Uint8Array([4])], "b.mov", { lastModified: 2 })),
  ]);
  const fd = buildCreateFormData(
    state,
    { prompt: "  提示词  ", target_duration_ms: 35000, ratio: "9:16", lang: "zh" },
  );
  assert.equal(fd.get("prompt"), "提示词"); // strip
  assert.equal(fd.get("target_duration_ms"), "35000");
  assert.equal(fd.get("ratio"), "9:16");
  assert.equal(fd.get("lang"), "zh");
  const videos = fd.getAll("videos");
  assert.equal(videos.length, 2);
  assert.deepEqual([...videos.map((f) => f.name)].sort(), ["a.mp4", "b.mov"]);
});

test("Agent 后端选择:CONSOLE_BACKENDS 冻结契约 + validateParams/buildCreateFormData", () => {
  // 白名单与后端 CONSOLE_FORM_BACKENDS 一致(stub 永不可由浏览器指定)
  assert.deepEqual([...CONSOLE_BACKENDS], ["claude-cli", "mcode-cli"]);

  const base = { prompt: "p", target_duration_ms: 35000, ratio: "9:16", lang: "zh" };
  // 缺省/空串 = 跟随服务端默认,合法
  assert.equal(validateParams(base, SMALL_LIMITS).backend, "");
  assert.equal(validateParams({ ...base, llm_backend: "" }, SMALL_LIMITS).backend, "");
  // 白名单内合法
  assert.equal(validateParams({ ...base, llm_backend: "claude-cli" }, SMALL_LIMITS).backend, "");
  assert.equal(validateParams({ ...base, llm_backend: "mcode-cli" }, SMALL_LIMITS).backend, "");
  // 白名单外(含 stub)报错
  assert.match(validateParams({ ...base, llm_backend: "stub" }, SMALL_LIMITS).backend, /Agent/);
  assert.match(validateParams({ ...base, llm_backend: "gpt-4" }, SMALL_LIMITS).backend, /Agent/);

  // FormData:非空才追加(旧契约向后兼容:不带字段 = 服务端默认)
  const state = createConsoleState();
  addFiles(state, [makeFileEntry(new File([new Uint8Array([1])], "a.mp4", { lastModified: 1 }))]);
  const fdWith = buildCreateFormData(state, { ...base, llm_backend: "claude-cli" });
  assert.equal(fdWith.get("llm_backend"), "claude-cli");
  const fdWithout = buildCreateFormData(state, base);
  assert.equal(fdWithout.get("llm_backend"), null);
});

test("friendlyError:已知码给稳定提示,未知码兜底,detail 截断", () => {
  assert.match(friendlyError(503, "PG down").message, /PostgreSQL/);
  assert.match(friendlyError(415, "").message, /不支持/);
  const unknown = friendlyError(418, "x");
  assert.match(unknown.message, /创建失败/);
  const long = friendlyError(500, "d".repeat(500));
  assert.equal(long.detail.length, 200);
  assert.match(friendlyError(0, "网络错误").message, /创建失败/);
});

test("formatBytes / formatDuration", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(1024), "1.0 KB");
  assert.equal(formatBytes(2 * 1024 ** 3), "2.0 GB");
  assert.equal(formatBytes(-1), "—");
  assert.equal(formatDuration(35000), "35.0s");
  assert.equal(formatDuration(null), "—");
  assert.equal(formatDuration(0), "—");
});

test("CONSOLE_API_PATHS 与后端路由契约一致", () => {
  assert.equal(CONSOLE_API_PATHS.config, "/api/console/config");
  assert.equal(CONSOLE_API_PATHS.csrf, "/api/console/csrf");
  assert.equal(CONSOLE_API_PATHS.createRun, "/api/console/runs");
  const tid = "console-abcdef123456";
  assert.equal(
    CONSOLE_API_PATHS.resumeStatus(tid),
    "/api/workflow/runs/console-abcdef123456/resume-status",
  );
  assert.equal(
    CONSOLE_API_PATHS.monitorPage(tid),
    "/?thread_id=console-abcdef123456",
  );
});

test("DEFAULT_LIMITS 与服务端 config.py 默认一致", () => {
  assert.equal(DEFAULT_LIMITS.max_videos, 10);
  assert.equal(DEFAULT_LIMITS.max_video_bytes, 2 * 1024 ** 3);
  assert.equal(DEFAULT_LIMITS.prompt_max_chars, 4000);
  assert.equal(DEFAULT_LIMITS.target_duration_ms_default, 35000);
  assert.equal(DEFAULT_LIMITS.target_duration_ms_max, 600000);
  assert.deepEqual(DEFAULT_LIMITS.allowed_video_exts, [".mp4", ".mov"]);
  assert.deepEqual(DEFAULT_LIMITS.ratios, ["9:16", "16:9", "4:3"]);
  assert.deepEqual(DEFAULT_LIMITS.langs, ["zh", "en"]);
});

test("re-export 连通:applyEvent 可归约 run.started(与监控同一套 reducer)", () => {
  const monitor = createMonitorState();
  const { applied } = applyEvent(monitor, {
    id: 1, event_type: "run.started", occurred_at: "2026-11-06T00:00:00Z",
  });
  assert.equal(applied, true);
  assert.equal(monitor.run.state, "running");
});

// ---------------------------------------------------------------------------
// 阶段 5:历史 / 恢复 / 取消(纯逻辑)
// ---------------------------------------------------------------------------

test("CONSOLE_API_PATHS 阶段 5 路径与后端路由契约一致", () => {
  const tid = "console-abcdef123456";
  assert.equal(CONSOLE_API_PATHS.listRuns, "/api/console/runs");
  assert.equal(CONSOLE_API_PATHS.runDetail(tid), "/api/console/runs/console-abcdef123456");
  assert.equal(
    CONSOLE_API_PATHS.resumeRun(tid),
    "/api/console/runs/console-abcdef123456/resume",
  );
  assert.equal(
    CONSOLE_API_PATHS.cancelRun(tid),
    "/api/console/runs/console-abcdef123456/cancel",
  );
  assert.equal(
    CONSOLE_API_PATHS.deleteRun(tid),
    "/api/console/runs/console-abcdef123456/delete",
  );
});

test("canResume / canCancel 状态门禁(waiting_human 是唯一可恢复态)", () => {
  assert.equal(canResume("waiting_human"), true);
  for (const s of ["prepared", "starting", "running", "completed", "failed", "cancelled", "unknown"]) {
    assert.equal(canResume(s), false, s);
  }
  assert.equal(canCancel("running"), true);
  assert.equal(canCancel("starting"), true);
  for (const s of ["prepared", "waiting_human", "completed", "failed", "cancelled", "unknown"]) {
    assert.equal(canCancel(s), false, s);
  }
});

test("canDelete 状态门禁(仅终态 + prepared;与服务端 DELETABLE_STATES 一致)", () => {
  for (const s of ["completed", "failed", "cancelled", "prepared"]) {
    assert.equal(canDelete(s), true, s);
  }
  for (const s of ["starting", "running", "waiting_human", "unknown"]) {
    assert.equal(canDelete(s), false, s);
  }
});

test("historyActions 按状态给操作提示", () => {
  assert.deepEqual(historyActions({ state: "waiting_human" }), { resume: true, cancel: false, delete: false });
  assert.deepEqual(historyActions({ state: "running" }), { resume: false, cancel: true, delete: false });
  assert.deepEqual(historyActions({ state: "completed" }), { resume: false, cancel: false, delete: true });
  assert.deepEqual(historyActions({ state: "prepared" }), { resume: false, cancel: false, delete: true });
  assert.deepEqual(historyActions(null), { resume: false, cancel: false, delete: false });
});

test("filterHistoryRows:空查询原样返回;按 thread_id/状态/中文词/时间过滤", () => {
  const rows = [
    { thread_id: "console-aaaaaaaaaaaa", state: "completed", created_at: "2026-12-01T00:00:00+00:00" },
    { thread_id: "console-bbbbbbbbbbbb", state: "running", created_at: "2026-12-02T00:00:00+00:00" },
    { thread_id: "console-cccccccccccc", state: "waiting_human", created_at: "2026-12-03T00:00:00+00:00" },
  ];
  assert.equal(filterHistoryRows(rows, "").length, 3);
  assert.equal(filterHistoryRows(rows, "bbbb").length, 1);
  assert.equal(filterHistoryRows(rows, "RUNNING").length, 1); // 大小写不敏感
  assert.equal(filterHistoryRows(rows, "等待人工").length, 1); // 中文状态词
  assert.equal(filterHistoryRows(rows, "12-02").length, 1); // 创建时间
  assert.equal(filterHistoryRows(rows, "不存在的关键字").length, 0);
  assert.deepEqual(filterHistoryRows(null, "x"), []);
  // 不修改原数组
  assert.equal(rows.length, 3);
});

test("HISTORY_STATE_LABEL 覆盖全部调和态且 waiting_human 不译作失败", () => {
  for (const s of ["prepared", "starting", "running", "waiting_human",
    "completed", "failed", "cancelled", "unknown"]) {
    assert.ok(HISTORY_STATE_LABEL[s], s);
  }
  assert.equal(HISTORY_STATE_LABEL.waiting_human, "等待人工关卡");
});

test("parseResumeValue:JSON 优先 → true/false 字面量 → 非法报错;空 = 默认 true", () => {
  assert.deepEqual(parseResumeValue(""), { ok: true, value: true, error: "" });
  assert.deepEqual(parseResumeValue("true"), { ok: true, value: true, error: "" });
  assert.deepEqual(parseResumeValue("false"), { ok: true, value: false, error: "" });
  assert.deepEqual(parseResumeValue('{"answer":"ok"}'), { ok: true, value: { answer: "ok" }, error: "" });
  assert.deepEqual(parseResumeValue("42"), { ok: true, value: 42, error: "" });
  assert.equal(parseResumeValue(" 42 ").value, 42); // 容忍首尾空白
  const bad = parseResumeValue("继续");
  assert.equal(bad.ok, false);
  assert.match(bad.error, /JSON/);
  assert.equal(parseResumeValue("继续").value, null);
});
