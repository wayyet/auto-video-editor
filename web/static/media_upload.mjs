// /static/media_upload.mjs
//
// 「Web UI 上传入口合并」的**纯策略层**。
//
// 背景:左下「回形针」(#uploadBtn/#fileInput) 与左上「导入视频」(#importVideoBtn)
// 的上传底层本来就是同一套(uploadMediaChunked → media/init → chunk×N → complete)。
// 合并后 importVideoFiles() 成为**唯一**上传编排入口,这里把其中不碰 DOM 的部分
// 抽成纯函数,于是可以:
//
//   1. 在 Node 里直接跑单测(不需要浏览器),见 web/tests/media_upload.test.mjs;
//   2. 让「按钮选择」和「拖拽上传」走同一条分类/限流/握手代码路径。
//
// 关键分流:视频与图片共用分片上传,但**只有工作流白名单内的视频**参加
// /api/system/import-video 握手 —— 图片与旧版通用视频止步会话 pending 素材区,
// 绝不能混进 video_input_paths。

// -- 白名单常量 -----------------------------------------------------------

/** 工作流视频白名单。必须与 nodes/node_04a_import_video.py::ACCEPT_EXTS 保持一致。 */
export const WORKFLOW_VIDEO_EXTS = Object.freeze([".mp4", ".mov", ".mkv", ".avi", ".webm"]);

/**
 * 旧回形针前端的视频扩展名兜底集合(含工作流白名单 + .m4v/.flv/.wmv)。
 * 不在 ACCEPT_EXTS 内的旧格式只走普通会话素材,不进工作流握手。
 */
export const GENERIC_VIDEO_EXTS = Object.freeze([
  ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv", ".wmv",
]);

/** 图片扩展名(旧回形针前端接受的集合;后端 detect_media_kind 只认其中一部分,此处不扩大后端承诺)。 */
export const IMAGE_EXTS = Object.freeze([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"]);

/** 音频扩展名:后端没有音频处理逻辑,不上传。 */
export const AUDIO_EXTS = Object.freeze([".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"]);

// -- 分类 -----------------------------------------------------------------

/** 取小写扩展名(含点);无扩展名返回 ""。 */
export function fileExt(name) {
  const s = String(name || "");
  const idx = s.lastIndexOf(".");
  if (idx < 0 || idx === s.length - 1) return "";
  return s.slice(idx).toLowerCase();
}

/**
 * 单文件分类。返回:
 *   "workflow_video"  工作流白名单视频(进 pending + 进握手)
 *   "image"           图片(只进 pending)
 *   "generic_video"   旧版通用视频(只进 pending,保持旧回形针行为)
 *   "audio"           音频(拒收)
 *   "unsupported"     其他(拒收)
 *
 * 判定顺序刻意是 音频 → 图片 → 工作流视频 → 通用视频:
 * 先把音频挡掉,避免异常 MIME 让音频混进素材区;
 * 图片优先于视频扩展名;`accept` 属性只是界面提示,最终仍以此函数为准。
 */
export function classifyMediaFile(f) {
  if (!f) return "unsupported";
  const type = String(f.type || "").toLowerCase();
  const ext = fileExt(f.name);
  if (type.startsWith("audio/") || AUDIO_EXTS.includes(ext)) return "audio";
  if (type.startsWith("image/") || IMAGE_EXTS.includes(ext)) return "image";
  if (WORKFLOW_VIDEO_EXTS.includes(ext)) return "workflow_video";
  if (type.startsWith("video/") || GENERIC_VIDEO_EXTS.includes(ext)) return "generic_video";
  return "unsupported";
}

// -- 批量规划 -------------------------------------------------------------

/**
 * 把一次选择的文件规划成上传批次。
 *
 * 返回:
 *   uploadList            [{ file, kind, isWorkflowVideo }] —— **保持用户选择顺序**,
 *                         只含可上传条目(工作流视频 + 图片 + 旧版通用视频)
 *   rejectedAudio         被拒音频数
 *   rejectedUnsupported   其他被拒文件数
 */
export function planUploadBatch(files) {
  const uploadList = [];
  let rejectedAudio = 0;
  let rejectedUnsupported = 0;
  for (const f of Array.from(files || [])) {
    const kind = classifyMediaFile(f);
    if (kind === "workflow_video") {
      uploadList.push({ file: f, kind, isWorkflowVideo: true });
    } else if (kind === "image" || kind === "generic_video") {
      uploadList.push({ file: f, kind, isWorkflowVideo: false });
    } else if (kind === "audio") {
      rejectedAudio += 1;
    } else {
      rejectedUnsupported += 1;
    }
  }
  return { uploadList, rejectedAudio, rejectedUnsupported };
}

/**
 * 按会话 pending 剩余容量截断上传批次(先到先得,保持选择顺序)。
 *
 * 返回 { batch, truncated }。旧实现对 `const candidates` 重新赋值会抛
 * TypeError —— 修复点收敛在这里,并用单测锁住。
 */
export function limitUploadBatch(uploadList, remain) {
  const list = Array.isArray(uploadList) ? uploadList : [];
  const cap = Math.max(0, Number(remain) || 0);
  return { batch: list.slice(0, cap), truncated: list.length > cap };
}

// -- 工作流握手载荷 --------------------------------------------------------

/**
 * 构造 POST /api/system/import-video 的载荷。
 *
 * `uploaded` 是上传成功的工作流视频 [{ media, file }]。载荷保持既有兼容结构:
 * `files` 数组 + 单值字段(= 第一个文件),后端 parse_import_payload 两种都认。
 *
 * `stored_path` 允许为空:public_media 不保证把磁盘路径暴露给浏览器,
 * 后端 resolve_session_media_paths() 会用 media_id 从会话媒体登记表解析真实路径。
 * 不要为了填充它而把服务器绝对路径暴露给前端。
 */
export function buildImportVideoPayload(sessionId, uploaded) {
  const files = (Array.isArray(uploaded) ? uploaded : []).map(({ media, file }) => ({
    media_id: (media && media.id) || "",
    // 后端 public_media 的字段是 name(不是 filename),两者都兜一下
    filename: (media && (media.name || media.filename)) || (file && file.name) || "",
    stored_path: (media && (media.path || media.stored_path)) || "",
  }));
  const first = files.length ? files[0] : { media_id: "", filename: "", stored_path: "" };
  return {
    session_id: String(sessionId || ""),
    files,
    media_id: first.media_id,
    filename: first.filename,
    stored_path: first.stored_path,
  };
}
