const { app, BrowserWindow, Menu, dialog, ipcMain, shell, session } = require("electron");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { createInterface } = require("node:readline");
const { fileURLToPath } = require("node:url");

const appName = "停车报表工作台";
const project = path.resolve(__dirname, "..");
const rendererFile = path.join(__dirname, "renderer", "index.html");
const smokeResult = process.argv.find((argument) => argument.startsWith("--smoke-output="))?.slice(15);
const smoke = !!smokeResult;
if (smoke) app.disableHardwareAcceleration();
const userDir = smoke ? fs.mkdtempSync(path.join(os.tmpdir(), "parking-workbench-smoke-"))
  : path.join(app.getPath("appData"), "ParkingReportWorkbench");
app.setPath("userData", userDir);
app.setName(appName);

const profile = path.join(userDir, "browser");
const settingsFile = path.join(userDir, "settings.json");
const smokeSettings = smoke && process.argv.find((argument) => argument.startsWith("--smoke-settings="))?.slice(17);
if (smokeSettings) fs.copyFileSync(smokeSettings, settingsFile);
const taskDir = path.join(userDir, "tasks");
const portableHome = process.env.PORTABLE_EXECUTABLE_DIR || (app.isPackaged ? path.dirname(process.execPath) : project);
const defaultOutput = path.resolve(portableHome);
const allowedFinal = new Set(["complete", "partial", "cancelled", "error"]);
const measureFields = ["completed", "total", "records", "channels"];
let appWindow;
let activeChild;
let action = "";
let activeCancellation;
let closing = false;
let allowQuit = false;
let job = null;
let auth = { status: "unverified", message: "请先验证停车平台浏览器会话。" };

function readSettings() {
  try {
    const saved = JSON.parse(fs.readFileSync(settingsFile, "utf8"));
    const history = Array.isArray(saved.history) ? saved.history.filter((item) =>
      item && typeof item.id === "string" && typeof item.outputDir === "string").slice(0, 200) : [];
    return { outputDir: saved.outputDir, workers: saved.workers,
      history: history.map((item) => allowedFinal.has(item.status)
        ? item : { ...item, status: "error", message: "上次任务结束前程序已退出。" }) };
  } catch {
    return { history: [] };
  }
}

const config = readSettings();
config.history = config.history.map(recoverHistoryItem);

function saveSettings() {
  const temporary = settingsFile + ".tmp";
  fs.mkdirSync(userDir, { recursive: true });
  fs.writeFileSync(temporary, JSON.stringify(config, null, 2), "utf8");
  fs.renameSync(temporary, settingsFile);
}

function fullState() {
  return {
    auth,
    defaults: {
      outputDir: config.outputDir || defaultOutput,
      workers: config.workers || 1,
    },
    job,
    history: config.history,
  };
}

function publish() {
  if (appWindow && !appWindow.isDestroyed()) appWindow.webContents.send("parking:state", fullState());
}

function safeIPC(name, callback) {
  ipcMain.handle("parking:" + name, (event, argument) => {
    const frame = event.senderFrame;
    const mainFrame = event.sender.mainFrame;
    let samePage = false;
    if (frame && frame.url === event.sender.getURL()) {
      try {
        const localPage = fileURLToPath(frame.url);
        samePage = path.basename(localPage) === "index.html" && path.basename(path.dirname(localPage)) === "renderer";
      } catch { /* non-file navigation is never trusted */ }
    }
    if (!appWindow || event.sender !== appWindow.webContents || !samePage || !mainFrame ||
        frame.processId !== mainFrame.processId || frame.routingId !== mainFrame.routingId) {
      if (smoke) throw new Error("页面来源验证失败。" + JSON.stringify({
        frameUrl: frame?.url, contentsUrl: event.sender.getURL(),
        frameId: frame?.routingId, mainFrameId: mainFrame?.routingId,
      }));
      throw new Error("页面来源验证失败。");
    }
    return callback(argument);
  });
}

function workerArgs(args) {
  if (app.isPackaged) {
    return [path.join(process.resourcesPath, "worker", "ParkingReportWorker.exe"), args];
  }
  return [
    process.env.PARKING_WORKER_PYTHON || path.join(project, "..", ".venv", "Scripts", "python.exe"),
    ["-u", "-X", "utf8", path.join(project, "worker", "main.py"), ...args],
  ];
}

function browserPath() {
  const bundled = path.join(app.isPackaged ? process.resourcesPath : path.join(project, "build"), "chromium", "chrome.exe");
  return fs.existsSync(bundled) ? bundled : undefined;
}

function runWorker(args, onEvent) {
  return new Promise((resolve) => {
    const [program, argv] = workerArgs(args);
    const child = childProcess.spawn(program, argv, {
      windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
    });
    activeChild = child;
    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    lines.on("line", (line) => {
      try {
        const event = JSON.parse(line);
        if (event && typeof event.type === "string") onEvent(event);
      } catch { /* worker diagnostics without a protocol envelope aren’t app events */ }
    });
    child.stderr.resume();
    child.once("error", (error) => {
      onEvent({ type: "error", message: error.code === "ENOENT" ? "找不到导出工作程序。" : "无法启动导出工作程序。" });
    });
    child.once("close", (code) => {
      if (activeChild === child) activeChild = null;
      lines.close();
      resolve(code);
    });
  });
}

function nonEmptyText(value, fallback = "") {
  return typeof value === "string" && value.trim() ? value.trim().slice(0, 700) : fallback;
}

function resultIssues(value) {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const name = nonEmptyText(item?.name);
    return name ? [{ name, state: nonEmptyText(item.state, "pending"), reason: nonEmptyText(item.reason) }] : [];
  });
}

function readManifest(item) {
  try {
    const data = JSON.parse(fs.readFileSync(path.join(item.outputDir, "入场通道导出进度.json"), "utf8"));
    if (data.scope?.start === item.start && data.scope?.end === item.end && data.scope?.status === null
        && data.scope?.entry_channel === "each_selectable" && Array.isArray(data.entries)) return data.entries;
  } catch { /* an interrupted run may not yet have written a manifest */ }
  return null;
}

function manifestIssues(entries) {
  return resultIssues(entries.filter((item) => item && !["exported", "empty"].includes(item.state))
    .map((item) => ({name: item.source_name, state: item.state, reason: item.note})));
}

function applyResult(target, event) {
  for (const key of measureFields) {
    if (Number.isInteger(event[key]) && event[key] >= 0) target[key] = event[key];
  }
  const workbook = resolveArtifact(event.ledgerPath, target.outputDir);
  target.summary = resolveArtifact(event.summaryPath, target.outputDir) || target.summary || "";
  target.workbook = workbook || target.workbook || "";
  target.status = allowedFinal.has(event.status) ? event.status : "error";
  target.issues = resultIssues(event.issues);
  target.resumable = typeof event.resumable === "boolean" ? event.resumable : target.status !== "complete";
  target.metrics = event.metrics;
  target.problem = nonEmptyText(event.problem);
  if (target.status === "complete" && !workbook) {
    target.status = "partial";
    target.problem = "找不到本次任务生成的台账文件。";
    target.issues.push({ name: "台账文件", state: "ledger_failed", reason: target.problem });
    target.resumable = !!readManifest(target);
  }
  const remaining = Math.max(0, target.total - target.completed);
  target.message = target.problem && event.status === "complete" ? target.problem : nonEmptyText(event.message) || {
    complete: "导出和台账校核已完成。",
    partial: remaining ? "任务已完成，有 " + remaining + " 个车场未完成，原因见问题明细。"
      : "任务已完成，部分结果存在问题，原因见问题明细。",
    cancelled: "任务已取消，已下载的报表已保存。",
    error: "后台导出未完成。",
  }[target.status];
}

function recoverHistoryItem(item) {
  if (item.status === "complete") return item;
  const entries = readManifest(item);
  if (!entries) return { ...item, resumable: false };
  const file = path.join(item.outputDir, "任务结果.json");
  try {
    const modified = fs.statSync(file).mtimeMs;
    if (!item.finishedAt || modified > Date.parse(item.finishedAt)) {
      const result = JSON.parse(fs.readFileSync(file, "utf8"));
      if (result.type === "result" && result.total === entries.length) {
        const recovered = { ...item, finishedAt: new Date(modified).toISOString() };
        applyResult(recovered, result);
        return recovered;
      }
    }
  } catch { /* pre-upgrade tasks have a manifest but no terminal receipt */ }
  if (Array.isArray(item.issues) && item.issues.length) {
    return { ...item, issues: resultIssues(item.issues), resumable: item.resumable !== false };
  }
  const issues = manifestIssues(entries);
  const channels = entries.flatMap((entry) => Array.isArray(entry?.channels) ? entry.channels : [])
    .filter((channel) => channel?.state === "exported");
  const records = channels.reduce((count, channel) => count + (Number.isInteger(channel.expected_rows)
    && channel.expected_rows >= 0 ? channel.expected_rows : 0), 0);
  return { ...item, total: entries.length, completed: entries.length - issues.length, channels: channels.length,
    records, issues, resumable: item.resumable !== false };
}

function isDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(value + "T00:00:00Z");
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function validateRequest(request) {
  if (!request || !isDate(request.start) || !isDate(request.end) || request.start > request.end) {
    throw new Error("请选择有效的起止日期，开始日期不能晚于结束日期。");
  }
  if (typeof request.ledger !== "string" || !path.isAbsolute(request.ledger)
      || path.extname(request.ledger).toLowerCase() !== ".xlsx" || path.basename(request.ledger).startsWith("~$")) {
    throw new Error("请选择有效的 Excel 台账（.xlsx）。");
  }
  if (!fs.statSync(request.ledger, { throwIfNoEntry: false })?.isFile()) throw new Error("找不到所选台账。");
  if (typeof request.outputDir !== "string" || !path.isAbsolute(request.outputDir)) {
    throw new Error("请选择有效的输出目录。");
  }
  if (![1, 2, 3, 4].includes(request.workers)) throw new Error("并行车场数须为 1 至 4。");
  return { ledger: path.resolve(request.ledger), start: request.start, end: request.end,
    outputDir: path.resolve(request.outputDir), workers: request.workers };
}

function makeOutputFolder(request) {
  fs.mkdirSync(request.outputDir, { recursive: true });
  const label = request.start === request.end ? request.start : request.start + "至" + request.end;
  for (let index = 1; index <= 999; index += 1) {
    const folder = path.join(request.outputDir, label + (index === 1 ? "" : "_" + String(index).padStart(2, "0")));
    try {
      fs.mkdirSync(folder);
      return folder;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
    }
  }
  throw new Error("这个日期范围下已有过多任务目录，请改用其他输出目录。");
}

function resolveArtifact(candidate, base) {
  if (typeof candidate !== "string" || !path.isAbsolute(candidate) || path.extname(candidate).toLowerCase() !== ".xlsx") return "";
  const relative = path.relative(base, candidate);
  if (!relative || relative === ".." || relative.startsWith(".." + path.sep) || path.isAbsolute(relative)) return "";
  try {
    return fs.statSync(candidate, { throwIfNoEntry: false })?.isFile() ? path.resolve(candidate) : "";
  } catch {
    return "";
  }
}

function appendLog(message, level = "info") {
  job.logs.push({ time: new Date().toISOString(), level, message });
  if (job.logs.length > 100) job.logs.shift();
}

function rememberJob() {
  const { logs, ...historyItem } = job;
  config.history = [historyItem, ...config.history.filter((item) => item.id !== job.id)].slice(0, 200);
}

function recordJob() {
  rememberJob();
  saveSettings();
}

function updateAuth(event) {
  if (event.type === "error") {
    auth = { status: "error", message: nonEmptyText(event.message, "浏览器会话验证失败。") };
    return;
  }
  if (event.type === "auth") {
    const status = event.status === "verified" ? "verified" : event.status === "verifying" ? "verifying" : "error";
    auth = { status, message: nonEmptyText(event.message, status === "verified" ? "停车平台会话已验证。" : "停车平台会话需要验证。") };
  }
}

function handleJobEvent(event) {
  const message = nonEmptyText(event.message);
  for (const key of measureFields) {
    if (Number.isInteger(event[key]) && event[key] >= 0) job[key] = event[key];
  }
  if (event.type === "progress") {
    job.phase = nonEmptyText(event.phase, job.phase);
    job.current = nonEmptyText(event.current);
    if (message) job.message = message;
  }
  if (event.type === "issues") job.issues = resultIssues(event.issues);
  if (event.type === "log" && message) appendLog(message, nonEmptyText(event.level, "info"));
  if (event.type === "auth" && event.status !== "verified") updateAuth(event);
  publish();
}

function taskFailure(name, state, reason) {
  job.problem = reason;
  job.issues = [...resultIssues(job.issues), { name, state, reason }];
}

async function executeJob(requestFile, cancelFile) {
  let outcome;
  let code;
  try {
    recordJob();
    code = await runWorker(["run", "--request", requestFile], (event) => {
      if (event.type === "result" || event.type === "error") outcome = event;
      else handleJobEvent(event);
    });
  } catch (error) {
    outcome = { type: "error", message: nonEmptyText(error.message, "无法启动后台导出。") };
  }
  if (outcome?.type === "result") {
    applyResult(job, outcome);
  } else {
    const entries = readManifest(job);
    job.issues = entries ? manifestIssues(entries) : resultIssues(job.issues);
    job.resumable = !!entries;
    job.status = !outcome && job.status === "cancelling" ? "cancelled" : "error";
    job.message = job.status === "cancelled" ? "任务已停止，已下载报表可在输出目录查看。"
      : nonEmptyText(outcome?.message, "后台工作程序已退出（" + String(code ?? "无退出码") + "）。");
    if (job.status === "error") taskFailure("导出任务", "task_failed", job.message);
  }
  appendLog(job.message, job.status === "error" ? "error" : job.status === "partial" ? "warning" : "info");
  job.finishedAt = new Date().toISOString();
  try {
    recordJob();
  } catch (error) {
    const reason = nonEmptyText(error.message, "无法写入本地历史记录。");
    taskFailure("保存历史记录", "history_failed", reason);
    if (job.status === "complete") {
      job.status = "partial";
      job.message = "报表已保存，但历史记录写入失败。";
    }
    appendLog(reason, "error");
    rememberJob();
  } finally {
    // Temporary request/cancel files are local to the job. A cleanup failure
    // must not prevent the settled state from reaching the renderer.
    for (const file of [requestFile, cancelFile]) {
      try { fs.rmSync(file, { force: true }); } catch { /* working files have no effect on saved results */ }
    }
    activeCancellation = null;
    action = "";
    publish();
    if (closing) {
      allowQuit = true;
      app.quit();
    }
  }
}

function requestCancel() {
  if (!job || job.status !== "running" || !activeCancellation) return;
  fs.writeFileSync(activeCancellation, "", { flag: "a" });
  job.status = "cancelling";
  job.message = "正在停止安排后续导出并保存已完成的报表。";
  appendLog(job.message);
  publish();
}

function launchJob(input, prior = null) {
  if (action) throw new Error("当前任务正在运行。");
  if (auth.status !== "verified") throw new Error("请先验证停车平台会话。");
  action = "run";
  try {
    const request = validateRequest(input);
    const folder = prior ? path.resolve(prior.outputDir) : makeOutputFolder(request);
    fs.mkdirSync(taskDir, { recursive: true });
    const id = prior?.id || randomUUID();
    const requestFile = path.join(taskDir, id + ".json");
    const cancelFile = path.join(folder, ".cancel");
    if (prior) fs.rmSync(cancelFile, { force: true });
    activeCancellation = cancelFile;
    fs.writeFileSync(requestFile, JSON.stringify({ ...request, outputDir: folder, profile,
      browser: browserPath(), cancelFile }), "utf8");
    if (!prior) {
      config.outputDir = request.outputDir;
      config.workers = request.workers;
      if (request.outputDir === defaultOutput) delete config.outputDir;
    }
    job = {
      id, runId: randomUUID(), createdAt: prior?.createdAt || new Date().toISOString(), ledger: request.ledger,
      start: request.start, end: request.end, workers: request.workers, outputDir: folder,
      status: "running", message: prior ? "正在从原任务目录继续，仅处理未完成项。" : "正在读取台账地块名单。",
      completed: prior?.completed || 0, total: prior?.total || 0, records: prior?.records || 0,
      channels: prior?.channels || 0, logs: [], issues: [], summary: prior?.summary, workbook: prior?.workbook,
    };
    if (prior) appendLog(job.message);
    publish();
    void executeJob(requestFile, cancelFile);
    return fullState();
  } catch (error) {
    action = "";
    activeCancellation = null;
    throw error;
  }
}

function registerIPC() {
  safeIPC("bootstrap", fullState);
  safeIPC("new-export", () => {
    if (action === "run") throw new Error("当前任务仍在处理，请等任务结束后再新建。");
    job = null;
    return fullState();
  });
  safeIPC("pick-ledger", async () => {
    const result = await dialog.showOpenDialog(appWindow, { title: "选择停车台账",
      properties: ["openFile"], filters: [{ name: "Excel 台账", extensions: ["xlsx"] }] });
    return result.canceled ? null : result.filePaths[0];
  });
  safeIPC("pick-output", async () => {
    const result = await dialog.showOpenDialog(appWindow, { title: "选择输出目录",
      defaultPath: config.outputDir || defaultOutput, properties: ["openDirectory", "createDirectory"] });
    if (result.canceled) return null;
    config.outputDir = path.resolve(result.filePaths[0]);
    saveSettings();
    return config.outputDir;
  });
  safeIPC("verify", async () => {
    if (action) return fullState();
    action = "verify";
    try {
      auth = { status: "verifying", message: "正在检查浏览器会话。需要登录时会打开账号验证窗口。" };
      publish();
      const args = ["verify", "--profile", profile, "--interactive"];
      const chromium = browserPath();
      if (chromium) args.push("--browser", chromium);
      await runWorker(args, (event) => {
        updateAuth(event);
        publish();
      });
      if (auth.status === "verifying") auth = { status: "error", message: "后台工作程序未返回验证结果。" };
      publish();
      return fullState();
    } finally {
      action = "";
    }
  });
  safeIPC("start", (input) => launchJob(input));
  safeIPC("resume", (id) => {
    const original = typeof id === "string" && config.history.find((item) => item.id === id);
    if (!original || !["partial", "error", "cancelled"].includes(original.status)) throw new Error("找不到可续跑的任务。");
    const prior = recoverHistoryItem(original);
    if (prior.status === "complete" || !prior.resumable || !path.isAbsolute(prior.outputDir)) {
      throw new Error("这个任务当前没有可续跑的项目。");
    }
    return launchJob({ ledger: prior.ledger, start: prior.start, end: prior.end,
      outputDir: path.dirname(prior.outputDir), workers: prior.workers || 1 }, prior);
  });
  safeIPC("cancel", () => {
    requestCancel();
    return fullState();
  });
  safeIPC("open-path", async (requested) => {
    if (typeof requested !== "string" || !path.isAbsolute(requested)) throw new Error("文件路径无效。");
    const chosen = path.resolve(requested);
    const allowed = [config.outputDir || defaultOutput, ...[job, ...config.history].filter(Boolean)
      .flatMap((item) => [item.outputDir, item.workbook, item.summary])].filter(Boolean);
    if (!allowed.some((item) => path.resolve(item).toLowerCase() === chosen.toLowerCase())) {
      throw new Error("只能打开本程序生成的输出目录或文件。");
    }
    const error = await shell.openPath(chosen);
    if (error) throw new Error("保存位置或文件无法打开，请检查文件是否已移走。");
  });
}

function createWindow() {
  appWindow = new BrowserWindow({
    width: 1190, height: 820, minWidth: 680, minHeight: 515, show: false, backgroundColor: "#f1f3f1",
    title: appName,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true,
    },
  });
  appWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  appWindow.webContents.on("will-navigate", (event) => event.preventDefault());
  return appWindow.loadFile(rendererFile);
}

if (!smoke && !app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => { if (appWindow && !appWindow.isDestroyed()) { appWindow.restore(); appWindow.focus(); } });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", (event) => {
    if (!activeChild || allowQuit) return;
    event.preventDefault();
    if (closing) return;
    closing = true;
    requestCancel();
    const child = activeChild;
    const deadline = setTimeout(() => {
      if (activeChild === child && child.pid) childProcess.spawn("taskkill.exe",
        ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
    }, action === "run" ? 30_000 : 0);
    deadline.unref();
    child.once("close", () => {
      clearTimeout(deadline);
      if (action !== "run") {
        allowQuit = true;
        app.quit();
      }
    });
  });
  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);
    session.defaultSession.setPermissionRequestHandler((_, __, respond) => respond(false));
    registerIPC();
    await createWindow();
    if (!smoke) {
      appWindow.show();
      return;
    }
    const state = await appWindow.webContents.executeJavaScript("window.parking.bootstrap()");
    const body = await appWindow.webContents.executeJavaScript("document.body.innerText");
    const draft = await appWindow.webContents.executeJavaScript(
      "Object.fromEntries(['ledger-path','date-start','date-end','output-path','worker-count']"
      + ".map(id => [id, document.getElementById(id).value]))");
    const workerChoices = await appWindow.webContents.executeJavaScript(
      "Array.from(document.getElementById('worker-count').options, option => Number(option.value))");
    const [program, args] = workerArgs(["--help"]);
    const worker = childProcess.spawnSync(program, args, { windowsHide: true, encoding: "utf8", timeout: 30_000 });
    fs.writeFileSync(smokeResult, JSON.stringify({ title: appWindow.getTitle(), defaults: state.defaults,
      verified: state.auth.status, body, draft, workerChoices, workerExitCode: worker.status, workerHelp: worker.stdout?.slice(0, 500),
      canNewExport: await appWindow.webContents.executeJavaScript("typeof window.parking.newExport === 'function'"),
      canResume: await appWindow.webContents.executeJavaScript("typeof window.parking.resume === 'function'"),
      historyCount: state.history.length,
      latest: state.job && {status: state.job.status, completed: state.job.completed, total: state.job.total,
        channels: state.job.channels, records: state.job.records, issues: state.job.issues?.length,
        resumable: state.job.resumable, workbook: state.job.workbook},
      bundledBrowser: !!browserPath(), packaged: app.isPackaged }, null, 2), "utf8");
    try {
      const screenshot = await appWindow.webContents.capturePage();
      if (!screenshot.isEmpty()) fs.writeFileSync(smokeResult + ".png", screenshot.toPNG());
    } catch { /* offscreen capture depends on the graphics driver; IPC smoke has already run */ }
    app.quit();
  }).catch((error) => {
    if (smoke) fs.writeFileSync(smokeResult, JSON.stringify({ error: error.message }), "utf8");
    else dialog.showErrorBox(appName, "程序启动失败。\n" + error.message);
    app.exit(1);
  });
}
