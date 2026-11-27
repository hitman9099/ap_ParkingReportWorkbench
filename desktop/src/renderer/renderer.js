"use strict";

const api = window.parking;
const byId = (id) => document.getElementById(id);
const refs = {
  shellAuth: byId("shell-auth"),
  shellAuthText: byId("shell-auth-text"),
  authPanel: byId("auth-panel"),
  authMessage: byId("auth-message"),
  verify: byId("verify-button"),
  notice: byId("notice"),
  config: byId("config-fields"),
  configStatus: byId("config-status"),
  form: byId("export-form"),
  ledger: byId("ledger-path"),
  output: byId("output-path"),
  sourceOutput: byId("source-output-path"),
  sourceTaskLabel: byId("source-task-label"),
  sourceTask: byId("source-task-path"),
  sourceRaw: byId("source-raw-path"),
  pickLedger: byId("pick-ledger"),
  pickOutput: byId("pick-output"),
  dateStart: byId("date-start"),
  dateEnd: byId("date-end"),
  dateHint: byId("date-hint"),
  workers: byId("worker-count"),
  start: byId("start-button"),
  startHint: byId("start-hint"),
  status: byId("job-status"),
  phase: byId("job-phase"),
  progressNumber: byId("progress-number"),
  progress: byId("job-progress"),
  current: byId("job-current"),
  jobMessage: byId("job-message"),
  jobProblem: byId("job-problem"),
  parks: byId("metric-parks"),
  channels: byId("metric-channels"),
  records: byId("metric-records"),
  jobActions: byId("job-actions"),
  cancel: byId("cancel-button"),
  resume: byId("resume-button"),
  openBook: byId("job-open-workbook"),
  openSummary: byId("job-open-summary"),
  openFolder: byId("job-open-folder"),
  jobIssues: byId("job-issues"),
  jobIssueSummary: byId("job-issue-summary"),
  jobIssueDetails: byId("job-issue-details"),
  logScroll: byId("log-scroll"),
  logEmpty: byId("log-empty"),
  logs: byId("log-entries"),
  navWorkspace: byId("nav-workspace"),
  navHistory: byId("nav-history"),
  exportView: byId("export-view"),
  historyView: byId("history-view"),
  exportTitle: byId("export-title"),
  historyTitle: byId("history-title"),
  newFromHistory: byId("new-from-history"),
  historyCount: byId("history-count"),
  historyEmpty: byId("history-empty"),
  historyWrap: byId("history-table-wrap"),
  historyRows: byId("history-rows"),
  announcer: byId("status-announcer"),
  result: byId("result-dialog"),
  resultTitle: byId("result-title"),
  resultIcon: byId("result-icon"),
  resultMessage: byId("result-message"),
  resultProblem: byId("result-problem"),
  resultMetrics: byId("result-metrics"),
  resultParks: byId("result-parks"),
  resultChannels: byId("result-channels"),
  resultRecords: byId("result-records"),
  resultIssues: byId("result-issues"),
  resultDetails: byId("result-details"),
  resultClose: byId("result-close"),
};

const authLabels = {
  loading: ["正在读取登录状态", "neutral", "正在读取本机登录状态。", "验证浏览器"],
  unverified: ["登录未验证", "warning", "先验证浏览器登录。平台要求身份验证时，请在打开的浏览器内完成。", "验证浏览器"],
  verifying: ["正在验证登录", "working", "等待平台验证。如浏览器窗口已打开，请在窗口内完成登录。", "验证中…"],
  verified: ["登录已验证", "success", "平台会话已验证，可以选择台账和统计日期。", "重新验证"],
  error: ["登录验证失败", "danger", "平台会话验证失败，请重试。", "重新验证"],
};

const jobLabels = {
  idle: ["待开始", "neutral", "暂无任务"],
  running: ["运行中", "working", "正在导出"],
  cancelling: ["正在停止", "warning", "正在停止"],
  complete: ["已完成", "success", "任务已完成"],
  partial: ["已完成（有问题）", "warning", "任务已完成（有问题）"],
  cancelled: ["已停止", "warning", "任务已停止"],
  error: ["失败", "danger", "任务失败"],
};

const issueLabels = {
  missing: "未找到",
  unmatched: "未找到",
  ambiguous: "匹配待确认",
  no_channels: "没有可用通道",
  export_failed: "导出失败",
  failed: "导出失败",
  validation_failed: "核验未通过",
  auth_failed: "登录验证失败",
  platform_failed: "平台访问失败",
  task_failed: "任务处理失败",
  report_failed: "通道汇总失败",
  ledger_failed: "台账生成失败",
  artifact_missing: "结果文件不可用",
  pending: "待处理",
  unattempted: "待处理",
  matched: "待处理",
};
const resumableStatuses = new Set(["partial", "cancelled", "error"]);
const settledStatuses = new Set(["complete", ...resumableStatuses]);
const pendingIssueStates = new Set(["pending", "unattempted", "matched"]);

const phaseLabels = {
  starting: "正在启动",
  prepare: "准备任务",
  preparing: "准备任务",
  matching: "查找车场",
  match: "查找车场",
  catalog: "读取车场目录",
  planning: "读取台账与通道",
  discovering: "识别入场通道",
  export: "逐通道导出",
  exporting: "逐通道导出",
  downloading: "下载通道报表",
  verifying: "核验报表",
  merge: "合并报表",
  merging: "合并报表",
  reconcile: "核对停车时长",
  reconciling: "核对停车时长",
  workbook: "写入台账",
  writing: "写入台账",
  finalizing: "保存结果",
  complete: "校核完成",
};

const numberFormat = new Intl.NumberFormat("zh-CN");
const percentFormat = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 0 });
const dateTimeFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
  minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const timeFormat = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

let snapshot = { auth: { status: "loading" }, defaults: {}, job: null, history: [] };
let defaultsApplied = false;
let snapshotUpdates = 0;
let historySignature = null;
let logSignature = null;
let issueSignature = null;
let announceSignature = null;
let displayedRun = null;
const busy = new Set();

function setText(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

function clean(value) {
  return typeof value === "string" ? value.trim() : "";
}

function filename(file) {
  return clean(file).split(/[\\/]/).pop() || "—";
}

function displayCount(num) {
  return Number.isFinite(num) && num >= 0 ? numberFormat.format(num) : "—";
}

function displayTime(value, format) {
  if (typeof value === "string" && /^\d{2}:\d{2}(?::\d{2})?$/.test(value)) return value;
  if (typeof value !== "string" && typeof value !== "number") return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? clean(value) : format.format(date);
}

function statusOf(job) {
  if (!job) return "idle";
  const value = job.status;
  return Object.hasOwn(jobLabels, value) ? value : "running";
}

function authStatus() {
  const value = snapshot.auth?.status;
  return Object.hasOwn(authLabels, value) ? value : "unverified";
}

function jobActive() {
  return ["running", "cancelling"].includes(statusOf(snapshot.job)) || busy.has("start") || busy.has("resume");
}

function canResume(job) {
  return typeof api?.resume === "function" && Boolean(clean(job?.id)) &&
    job.resumable !== false && resumableStatuses.has(statusOf(job));
}

function setPath(target, value) {
  target.value = clean(value);
  target.title = target.value;
}

function setWorkerCount(count) {
  const value = String(Number(count));
  const options = Array.from(refs.workers.options);
  refs.workers.value = options.some((option) => option.value === value) ? value : "1";
}

function applyPreferences(defaults) {
  setPath(refs.output, defaults.outputDir);
  setWorkerCount(defaults.workers);
}

function setBadge(element, label, tone) {
  setText(element, label);
  element.dataset.tone = tone;
}

function showNotice(message = "") {
  const detail = clean(message);
  refs.notice.hidden = !detail;
  setText(refs.notice, detail);
}

function showView(view) {
  const history = view === "history";
  refs.exportView.hidden = history;
  refs.historyView.hidden = !history;
  refs.navWorkspace.removeAttribute("aria-current");
  refs.navHistory.removeAttribute("aria-current");
  (history ? refs.navHistory : refs.navWorkspace).setAttribute("aria-current", "page");
  (history ? refs.historyTitle : refs.exportTitle).focus({ preventScroll: false });
}

function appendDirectory(parent, child) {
  return parent.replace(/[\\/]+$/, "") + "\\" + child;
}

function renderSources() {
  const output = clean(refs.output.value);
  const start = refs.dateStart.value;
  const end = refs.dateEnd.value;
  const period = start && end && start <= end ? (start === end ? start : start + "至" + end) : "";
  const actual = clean(snapshot.job?.outputDir);
  const task = actual || (output && period ? appendDirectory(output, period) : "");
  setText(refs.sourceOutput, output || "选择输出目录后显示");
  setText(refs.sourceTaskLabel, actual ? "实际任务目录" : "计划任务目录");
  setText(refs.sourceTask, task || "选择统计日期后显示");
  setText(refs.sourceRaw, task ? appendDirectory(task, "原始报表") : "选择统计日期后显示");
}

function renderConfig() {
  const status = authStatus();
  const [label, tone, message, verifyLabel] = authLabels[status];
  refs.shellAuth.dataset.tone = tone;
  setText(refs.shellAuthText, label);
  refs.authPanel.dataset.tone = tone;
  setText(refs.authMessage, clean(snapshot.auth?.message) || message);
  setText(refs.verify, verifyLabel);
  refs.verify.disabled = status === "loading" || status === "verifying" ||
    busy.has("verify") || busy.has("new-export") || jobActive();
  const usable = status === "verified" && !jobActive() && !busy.has("new-export");
  refs.config.disabled = !usable;
  if (jobActive()) {
    setBadge(refs.configStatus, "任务运行中", "working");
  } else if (status === "verified") {
    setBadge(refs.configStatus, "可设置", "success");
  } else {
    setBadge(refs.configStatus, "待验证", "neutral");
  }

  const from = refs.dateStart.value;
  const to = refs.dateEnd.value;
  const invalid = Boolean(from && to && from > to);
  refs.dateHint.hidden = !invalid;
  refs.dateEnd.setAttribute("aria-invalid", String(invalid));
  const hasConfig = refs.ledger.value && from && to && refs.output.value && !invalid;
  refs.start.disabled = !usable || !hasConfig || busy.has("ledger") || busy.has("output");

  let hint = "";
  if (jobActive()) {
    hint = "当前任务结束后可新建导出。";
  } else if (status !== "verified") {
    hint = "验证登录后可开始设置。";
  } else if (!refs.ledger.value) {
    hint = "请选择台账文件。";
  } else if (!from || !to) {
    hint = "请选择完整的统计日期。";
  } else if (invalid) {
    hint = "请检查统计日期。";
  } else if (!refs.output.value) {
    hint = "请选择输出目录。";
  }
  setText(refs.startHint, hint);
  refs.pickLedger.disabled = busy.has("ledger");
  refs.pickOutput.disabled = busy.has("output");
  renderSources();
}

function currentLabel(value) {
  if (typeof value === "string") return clean(value);
  return [clean(value?.park), clean(value?.channel)].filter(Boolean).join(" · ");
}

function displayPhase(job, fallback) {
  const phase = clean(job?.phase);
  return Object.hasOwn(phaseLabels, phase) ? phaseLabels[phase] : /[\u3400-\u9fff]/.test(phase) ? phase : fallback;
}

function renderLogs(items) {
  const records = Array.isArray(items) ? items.slice(-200) : [];
  const signature = JSON.stringify(records);
  if (signature === logSignature) return;
  logSignature = signature;
  const scrolledToEnd = refs.logScroll.scrollHeight - refs.logScroll.clientHeight - refs.logScroll.scrollTop < 20;
  const previousPosition = refs.logScroll.scrollTop;
  const fragment = document.createDocumentFragment();
  for (const log of records) {
    const message = typeof log === "string" ? clean(log) : clean(log?.message);
    if (!message) continue;
    const item = document.createElement("li");
    item.className = "log-entry";
    item.dataset.level = typeof log === "object" && log ? clean(log.level) : "";
    const time = displayTime(log?.time, timeFormat);
    if (time) {
      const stamp = document.createElement("time");
      stamp.textContent = time;
      if (typeof log.time === "string") stamp.dateTime = log.time;
      item.append(stamp);
    }
    const text = document.createElement("span");
    text.textContent = message;
    item.append(text);
    fragment.append(item);
  }
  refs.logEmpty.hidden = Boolean(fragment.childElementCount);
  refs.logs.replaceChildren(fragment);
  refs.logScroll.scrollTop = scrolledToEnd ? refs.logScroll.scrollHeight : previousPosition;
}

function issuesOf(job, includePending = true) {
  const raw = Array.isArray(job?.issues) ? job.issues : [];
  return raw.filter((item) => clean(item?.name) && (includePending || !pendingIssueStates.has(item.state))).map((item) => ({
    name: clean(item.name).replace(/\s+/g, " "),
    state: Object.hasOwn(issueLabels, item.state) ? issueLabels[item.state] : "待核对",
    reason: clean(item.reason),
  }));
}

function issueTally(issues) {
  const tally = new Map();
  for (const issue of issues) tally.set(issue.state, (tally.get(issue.state) || 0) + 1);
  return [...tally].map(([state, count]) => state + " " + displayCount(count) + " 项").join(" · ");
}

function issueList(issues) {
  const list = document.createElement("ol");
  list.className = "issue-list";
  for (const issue of issues) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.className = "issue-name";
    name.textContent = issue.name + " · " + issue.state;
    item.append(name);
    if (issue.reason) {
      const reason = document.createElement("span");
      reason.textContent = issue.reason;
      item.append(reason);
    }
    list.append(item);
  }
  return list;
}

function issueDetails(issues) {
  const details = document.createElement("details");
  details.className = "issue-details";
  const summary = document.createElement("summary");
  summary.textContent = "查看问题明细";
  details.append(summary, issueList(issues));
  return details;
}

function taskMessage(job, status) {
  return clean(job?.message) || {
    idle: "开始导出后，可在这里查看任务进展。",
    complete: "导出和台账校核已完成。",
    partial: "本次任务已结束，完成情况和问题见下方。",
    cancelled: "任务已停止。",
    error: "任务未能完成，请查看问题或任务日志。",
  }[status] || "";
}

function overallProblem(job) {
  const problem = clean(job?.problem);
  return problem === clean(job?.message) ? "" : problem;
}

function displayParks(job) {
  const total = job?.total;
  const completed = job?.completed;
  return Number.isFinite(total) && total > 0
    ? displayCount(completed ?? 0) + " / " + displayCount(total)
    : displayCount(completed);
}

function renderIssues(issues, unfinished) {
  const signature = JSON.stringify([issues, unfinished]);
  if (signature === issueSignature) return;
  issueSignature = signature;
  refs.jobIssues.hidden = !issues.length && !unfinished;
  setText(refs.jobIssueSummary, issues.length ? issueTally(issues) : "未完成车场详情可在汇总表或任务日志中查看。");
  refs.jobIssueDetails.replaceChildren(...(issues.length ? [issueDetails(issues)] : []));
}

function renderJob() {
  const job = snapshot.job;
  const status = statusOf(job);
  const [label, tone, defaultPhase] = jobLabels[status];
  const active = ["running", "cancelling"].includes(status);
  const completed = Number.isFinite(job?.completed) && job.completed >= 0 ? job.completed : null;
  const total = Number.isFinite(job?.total) && job.total > 0 ? job.total : null;
  const unfinished = total === null ? 0 : Math.max(0, total - (completed ?? 0));
  const terminal = settledStatuses.has(status);
  const issues = issuesOf(job, !active);
  const phase = active ? displayPhase(job, defaultPhase) : defaultPhase;
  setBadge(refs.status, label, tone);
  setText(refs.phase, phase);
  refs.progress.hidden = resumableStatuses.has(status);
  if (total !== null) {
    const progress = Math.min(total, completed ?? 0);
    refs.progress.max = total;
    refs.progress.value = progress;
    setText(refs.progressNumber, percentFormat.format(progress / total));
  } else {
    refs.progress.max = 1;
    if (active) refs.progress.removeAttribute("value");
    else refs.progress.value = status === "complete" ? 1 : 0;
    setText(refs.progressNumber, status === "complete" ? "100%" : "—");
  }
  if (terminal && status !== "complete") {
    setText(refs.progressNumber, unfinished ? "未完成 " + displayCount(unfinished) + " 个" : "");
  }
  const current = active ? currentLabel(job?.current) : "";
  const problem = overallProblem(job);
  refs.current.hidden = !current;
  setText(refs.current, current);
  setText(refs.jobMessage, taskMessage(job, status));
  refs.jobProblem.hidden = !problem;
  setText(refs.jobProblem, problem);
  setText(refs.parks, displayParks(job));
  setText(refs.channels, displayCount(job?.channels));
  setText(refs.records, displayCount(job?.records));
  refs.cancel.hidden = !active;
  refs.cancel.disabled = status === "cancelling" || busy.has("cancel");
  setText(refs.cancel, refs.cancel.disabled ? "正在停止…" : "停止任务");
  refs.resume.hidden = !canResume(job);
  refs.resume.disabled = jobActive() || busy.has("verify") || busy.has("new-export") || authStatus() === "verifying";
  setText(refs.resume, busy.has("resume") ? "正在继续…" : "继续未完成");
  refs.openBook.hidden = !clean(job?.workbook);
  refs.openSummary.hidden = !clean(job?.summary) || job.summary === job.workbook;
  refs.openFolder.hidden = !clean(job?.outputDir);
  refs.jobActions.hidden = refs.cancel.hidden && refs.resume.hidden && refs.openBook.hidden &&
    refs.openSummary.hidden && refs.openFolder.hidden;
  renderIssues(issues, terminal && status !== "complete" ? unfinished : 0);
  renderLogs(job?.logs);

  const key = status + "/" + phase;
  if (announceSignature !== key) {
    announceSignature = key;
    setText(refs.announcer, job ? [phase, terminal && unfinished ? "未完成 " + unfinished + " 个车场" : ""]
      .filter(Boolean).join("，") : "");
  }
}

function openResult({ title, tone, message, problem = "", job = null, issues = [] }) {
  refs.result.dataset.tone = tone;
  setText(refs.resultTitle, title);
  setText(refs.resultIcon, tone === "success" ? "✓" : tone === "danger" ? "×" : "!");
  setText(refs.resultMessage, message);
  refs.resultProblem.hidden = !problem;
  setText(refs.resultProblem, problem);
  refs.resultMetrics.hidden = !job;
  setText(refs.resultParks, displayParks(job));
  setText(refs.resultChannels, displayCount(job?.channels));
  setText(refs.resultRecords, displayCount(job?.records));
  refs.resultIssues.hidden = !issues.length;
  refs.resultIssues.replaceChildren();
  if (issues.length) {
    const summary = document.createElement("p");
    summary.className = "issue-summary";
    summary.textContent = issueTally(issues);
    refs.resultIssues.append(summary, issueList(issues));
  }
  refs.resultDetails.hidden = !job;
  if (!refs.result.open) refs.result.showModal();
  refs.resultClose.focus({ preventScroll: true });
}

function showSettledResult(job) {
  const state = statusOf(job);
  const runId = clean(job?.runId);
  if (!settledStatuses.has(state) || !runId || runId === displayedRun || !clean(job?.finishedAt)) return;
  displayedRun = runId;
  const [, tone, title] = jobLabels[state];
  openResult({ title, tone, job, message: taskMessage(job, state), problem: overallProblem(job), issues: issuesOf(job) });
}

function appendCell(row, primary, secondary = "", title = "") {
  const cell = document.createElement("td");
  const heading = document.createElement("div");
  heading.className = "history-primary";
  heading.textContent = primary;
  cell.append(heading);
  if (secondary) {
    const description = document.createElement("div");
    description.className = "history-secondary";
    description.textContent = secondary;
    cell.append(description);
  }
  if (title) cell.title = title;
  row.append(cell);
  return cell;
}

function historyLink(target, label) {
  const button = document.createElement("button");
  button.className = "button button-outline button-small";
  button.type = "button";
  button.textContent = label;
  button.title = target;
  button.addEventListener("click", () => openPath(target));
  return button;
}

function renderHistory() {
  const history = Array.isArray(snapshot.history) ? snapshot.history : [];
  const disableResume = jobActive() || busy.has("verify") || busy.has("new-export") || authStatus() === "verifying";
  const signature = JSON.stringify([history, disableResume]);
  if (historySignature === signature) return;
  historySignature = signature;
  refs.historyCount.hidden = !history.length;
  setText(refs.historyCount, displayCount(history.length));
  refs.historyEmpty.hidden = Boolean(history.length);
  refs.historyWrap.hidden = !history.length;

  const fragment = document.createDocumentFragment();
  const ordered = history.slice().sort((left, right) => {
    const leftTime = new Date(left.createdAt).getTime();
    const rightTime = new Date(right.createdAt).getTime();
    return Number.isFinite(leftTime) && Number.isFinite(rightTime) ? rightTime - leftTime : 0;
  });
  for (const record of ordered) {
    const row = document.createElement("tr");
    appendCell(row, displayTime(record.createdAt, dateTimeFormat) || "—");
    appendCell(row, filename(record.ledger), "", clean(record.ledger));
    appendCell(row, [clean(record.start), clean(record.end)].filter(Boolean).join(" 至 ") || "—");
    const result = document.createElement("td");
    const status = document.createElement("span");
    status.className = "badge";
    const state = statusOf(record);
    const [label, tone] = jobLabels[state];
    setBadge(status, label, tone);
    result.append(status);
    const tally = [];
    if (Number.isFinite(record.completed)) {
      tally.push(displayCount(record.completed) + (Number.isFinite(record.total) ? " / " + displayCount(record.total) : "") + " 个车场");
    }
    if (Number.isFinite(record.records)) tally.push(displayCount(record.records) + " 条记录");
    if (tally.length) {
      const summary = document.createElement("p");
      summary.className = "history-secondary";
      summary.textContent = tally.join(" · ");
      result.append(summary);
    }
    if (settledStatuses.has(state)) {
      const message = document.createElement("p");
      message.className = "history-secondary";
      message.textContent = taskMessage(record, state);
      result.append(message);
      const problem = overallProblem(record);
      if (problem) {
        const detail = document.createElement("p");
        detail.className = "history-secondary job-problem";
        detail.textContent = problem;
        result.append(detail);
      }
    }
    const issues = issuesOf(record, settledStatuses.has(state));
    if (issues.length) {
      const summary = document.createElement("p");
      summary.className = "history-secondary";
      summary.textContent = issueTally(issues);
      result.append(summary, issueDetails(issues));
    }
    row.append(result);

    const files = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "history-actions";
    if (canResume(record)) {
      const button = document.createElement("button");
      button.className = "button button-primary button-small";
      button.type = "button";
      button.textContent = "继续未完成";
      button.disabled = disableResume;
      button.addEventListener("click", () => resumeJob(record));
      actions.append(button);
    }
    if (clean(record.workbook)) actions.append(historyLink(record.workbook, "打开台账"));
    if (clean(record.summary) && record.summary !== record.workbook) {
      actions.append(historyLink(record.summary, "打开汇总"));
    }
    if (clean(record.outputDir)) actions.append(historyLink(record.outputDir, "打开目录"));
    if (!actions.childElementCount) actions.textContent = "—";
    files.append(actions);
    row.append(files);
    fragment.append(row);
  }
  refs.historyRows.replaceChildren(fragment);
}

function accept(value, announceResult = true) {
  if (!value || typeof value !== "object" || !Object.hasOwn(value, "auth")) return;
  snapshot = value;
  if (!defaultsApplied) {
    applyPreferences(snapshot.defaults || {});
    defaultsApplied = true;
  }
  renderConfig();
  renderJob();
  renderHistory();
  if (announceResult) showSettledResult(snapshot.job);
}

async function perform(action, handler) {
  if (busy.has(action)) return;
  showNotice();
  const runBeforeAction = snapshot.job?.runId;
  busy.add(action);
  renderConfig();
  renderJob();
  renderHistory();
  try {
    const result = await handler();
    accept(result);
    return result;
  } catch (error) {
    const detail = clean(error?.message).replace(/^Error invoking remote method '[^']+': (?:Error: )?/, "")
      || "操作未完成，请重试。";
    showNotice(detail);
    if ((action === "start" || action === "resume") && runBeforeAction === snapshot.job?.runId && !refs.result.open) {
      openResult({ title: "任务未开始", tone: "danger", message: detail });
    }
  } finally {
    busy.delete(action);
    renderConfig();
    renderJob();
    renderHistory();
  }
}

async function resumeJob(job) {
  if (jobActive() || busy.has("new-export") || !canResume(job)) return;
  showView("workspace");
  if (authStatus() !== "verified") {
    showNotice("请先验证浏览器登录，再继续任务。");
    refs.verify.focus();
    return;
  }
  const result = await perform("resume", () => api.resume(job.id));
  const current = result?.job;
  if (current?.id === job.id) {
    setPath(refs.ledger, current.ledger);
    refs.dateStart.value = clean(current.start);
    refs.dateEnd.value = clean(current.end);
    setWorkerCount(current.workers);
    renderConfig();
  }
}

async function newExport() {
  showView("workspace");
  if (jobActive()) {
    showNotice("当前任务仍在运行，结束后可新建导出。");
    return;
  }
  await perform("new-export", async () => {
    const result = await api.newExport();
    setPath(refs.ledger, "");
    refs.dateStart.value = "";
    refs.dateEnd.value = "";
    return result;
  });
}

async function openPath(target) {
  if (clean(target)) {
    await perform("open", async () => {
      const error = await api.openPath(target);
      if (typeof error === "string" && error) throw new Error(error);
    });
  }
}

async function selectPath(action, picker, input) {
  await perform(action, async () => {
    const selection = await picker();
    const path = typeof selection === "string" ? selection : selection?.path;
    if (clean(path)) setPath(input, path);
  });
}

refs.navWorkspace.addEventListener("click", newExport);
refs.navHistory.addEventListener("click", () => showView("history"));
refs.newFromHistory.addEventListener("click", newExport);
refs.verify.addEventListener("click", () => perform("verify", () => api.verify()));
refs.pickLedger.addEventListener("click", () => selectPath("ledger", () => api.pickLedger(), refs.ledger));
refs.pickOutput.addEventListener("click", () => selectPath("output", () => api.pickOutput(), refs.output));
refs.dateStart.addEventListener("input", renderConfig);
refs.dateEnd.addEventListener("input", renderConfig);
refs.cancel.addEventListener("click", () => perform("cancel", () => api.cancel()));
refs.resume.addEventListener("click", () => resumeJob(snapshot.job));
refs.openBook.addEventListener("click", () => openPath(snapshot.job?.workbook));
refs.openSummary.addEventListener("click", () => openPath(snapshot.job?.summary));
refs.openFolder.addEventListener("click", () => openPath(snapshot.job?.outputDir));
refs.resultClose.addEventListener("click", () => refs.result.close());
refs.resultDetails.addEventListener("click", () => {
  refs.result.close();
  showView("workspace");
  const details = refs.jobIssueDetails.querySelector("details");
  if (details) {
    details.open = true;
    details.querySelector("summary").focus();
  } else {
    refs.jobMessage.focus();
  }
});
refs.form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (refs.start.disabled) return;
  perform("start", () => api.start({
    ledger: refs.ledger.value,
    start: refs.dateStart.value,
    end: refs.dateEnd.value,
    outputDir: refs.output.value,
    workers: Number(refs.workers.value),
  }));
});

async function boot() {
  if (!api) {
    showNotice("无法连接客户端后台。请从停车报表工作台程序打开。");
    return;
  }
  try {
    const version = snapshotUpdates;
    const unsubscribe = api.subscribe((value) => {
      snapshotUpdates += 1;
      accept(value);
    });
    if (typeof unsubscribe === "function") addEventListener("beforeunload", unsubscribe, { once: true });
    const initial = await api.bootstrap();
    if (version === snapshotUpdates) accept(initial, false);
  } catch (error) {
    showNotice(clean(error?.message) || "无法读取本地设置。请重启客户端后重试。");
  }
}

boot();
