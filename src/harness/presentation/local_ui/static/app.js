// ローカル UI の画面側。
//
// * `innerHTML` を使わない。**外から来た文字列は必ず `textContent` で置く。**
//   Message 本文も Provider の備考も Error の理由も、すべて信用しない。
// * 外部 URL へ通信しない。`fetch` の宛先は相対 Path だけである。
// * Session Token は Header で運ぶ。URL にも Query にも載せない。
// * Provider が未設定のあいだ、送信 Button は無効のままにする。

"use strict";

const SESSION = document.querySelector('meta[name="harness-session"]').content;

let currentConversationId = null;
let currentConversationMessages = [];
let requestedSnapshotId = null;

function api(path, options) {
  const init = Object.assign({ headers: {} }, options || {});
  init.headers = Object.assign({}, init.headers);
  if (init.method && init.method !== "GET") {
    init.headers["Content-Type"] = "application/json";
    init.headers["X-Harness-Session"] = SESSION;
  }
  init.credentials = "omit";
  init.cache = "no-store";
  return fetch(path, init).then(function (response) {
    return response.json().then(function (payload) {
      return { status: response.status, payload: payload };
    });
  });
}

function post(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body || {}) });
}

function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value === null || value === undefined ? "" : String(value);
  if (className) {
    node.className = className;
  }
  return node;
}

function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function showError(node, result) {
  const error = result.payload && result.payload.error;
  if (!error) {
    node.textContent = "";
    return false;
  }
  const code = error.code ? " [" + error.code + "]" : "";
  node.textContent = result.status + code + " " + error.reason;
  return true;
}

// -- Provider ---------------------------------------------------------------

function renderProviders() {
  return api("/api/providers").then(function (result) {
    const payload = result.payload;
    document.getElementById("provider-message").textContent = payload.message || "";

    const blocking = document.getElementById("provider-blocking");
    clear(blocking);
    (payload.blocking_reasons || []).forEach(function (reason) {
      blocking.appendChild(text("li", reason, "note"));
    });

    const rows = document.getElementById("provider-rows");
    clear(rows);
    (payload.providers || []).forEach(function (provider) {
      const tr = document.createElement("tr");
      // CONFIGURED を READY_TO_SEND として出さない。別々の列にする。
      [
        provider.provider_id,
        "登録済み",
        provider.route_class,
        provider.enabled ? "true" : "false",
        provider.contract_state,
        "CONFIGURED",
        provider.endpoint_configured ? "CONFIGURED" : "OWNER_VALUE_MISSING",
        provider.model_configured ? "CONFIGURED" : "OWNER_VALUE_MISSING",
        provider.secret_ref_configured ? "CONFIGURED" : "OWNER_VALUE_MISSING",
        provider.api_status === "AVAILABLE" ? "IMPLEMENTED" : "NOT_IMPLEMENTED",
        "READY_TO_SEND: " + (provider.chat_send_allowed ? "true" : "false"),
        provider.note
      ].forEach(function (value) {
        tr.appendChild(text("td", value));
      });
      rows.appendChild(tr);
    });

    const button = document.getElementById("send-button");
    button.disabled = !payload.any_send_allowed;
    return payload;
  });
}

// -- 会話一覧 ---------------------------------------------------------------

function renderConversations() {
  return api("/api/conversations").then(function (result) {
    const errorNode = document.getElementById("list-error");
    if (showError(errorNode, result)) {
      return;
    }
    errorNode.textContent = "";
    const payload = result.payload;
    const storage = payload.storage || {};
    document.getElementById("storage-line").textContent =
      "保存先: " + (storage.kind || "") + "  DB: " + (storage.database || "");

    const rows = document.getElementById("conversation-rows");
    clear(rows);
    document.getElementById("conversation-empty").hidden = (payload.conversations || []).length > 0;
    (payload.conversations || []).forEach(function (conversation) {
      const tr = document.createElement("tr");
      tr.appendChild(text("td", conversation.display_name));
      tr.appendChild(text("td", conversation.conversation_id, "hash"));
      tr.appendChild(text("td", conversation.created_at));
      tr.appendChild(text("td", conversation.message_count));
      tr.appendChild(
        text("td", conversation.latest_snapshot ? conversation.latest_snapshot.snapshot_id : "なし")
      );
      tr.appendChild(text("td", conversation.hash_verified ? "一致" : "不一致"));
      tr.appendChild(text("td", conversation.stored_locally ? "ローカル" : "不明"));
      const open = text("button", "開く");
      open.type = "button";
      open.addEventListener("click", function () {
        openConversation(conversation.conversation_id);
      });
      const cell = document.createElement("td");
      cell.appendChild(open);
      tr.appendChild(cell);
      rows.appendChild(tr);
    });
  });
}

// -- 会話の詳細 -------------------------------------------------------------

function renderMessages(messages) {
  const rows = document.getElementById("message-rows");
  clear(rows);
  messages.forEach(function (message) {
    const tr = document.createElement("tr");
    tr.appendChild(text("td", message.sequence_number));
    tr.appendChild(text("td", message.role));
    tr.appendChild(text("td", message.created_at));
    if (message.body === null || message.body === undefined) {
      // **空文字列で代替しない。** 取れなかったことをそのまま出す。
      tr.appendChild(
        text("td", "本文取得不可（" + (message.body_reason || "理由不明") + "）", "unavailable")
      );
    } else {
      tr.appendChild(text("td", message.body));
    }
    tr.appendChild(text("td", message.content_artifact_hash, "hash"));
    tr.appendChild(text("td", message.content_hash, "hash"));
    rows.appendChild(tr);
  });
}

function renderSnapshots(snapshots) {
  const rows = document.getElementById("snapshot-rows");
  clear(rows);
  const select = document.getElementById("preview-snapshot");
  clear(select);
  snapshots.forEach(function (snapshot) {
    const tr = document.createElement("tr");
    tr.appendChild(text("td", snapshot.snapshot_id, "hash"));
    tr.appendChild(text("td", snapshot.snapshot_hash, "hash"));
    tr.appendChild(text("td", snapshot.message_set_hash, "hash"));
    tr.appendChild(text("td", snapshot.schema_set_hash, "hash"));
    tr.appendChild(text("td", snapshot.design_sha256, "hash"));
    tr.appendChild(text("td", snapshot.verified ? "一致" : "不一致"));
    rows.appendChild(tr);

    const option = text("option", snapshot.snapshot_id);
    option.value = snapshot.snapshot_id;
    select.appendChild(option);
  });
  if (requestedSnapshotId && snapshots.some(function (snapshot) { return snapshot.snapshot_id === requestedSnapshotId; })) {
    select.value = requestedSnapshotId;
  }
  document.getElementById("context-preview-submit").disabled = snapshots.length === 0;
  document.getElementById("conversation-progress").textContent = snapshots.length ?
    "本文と確認用コピーを保存しています。本文を追加した場合はコピーを作り直し、確認用のモデルと予算を選んで情報量を確認してください。" :
    "本文を保存したら「確認用コピーを作る」を押してください。その後、情報量の確認へ進めます。";
}

function renderRoles(roles) {
  const select = document.getElementById("role-select");
  if (select.options.length === roles.length) {
    return;
  }
  clear(select);
  // Role 名を画面側で書かない。Server が既存 Enum から返した値だけを並べる。
  roles.forEach(function (role) {
    const option = text("option", role);
    option.value = role;
    select.appendChild(option);
  });
  // The service supplies the role list; ordinary notes never default to a control role.
  if (roles.indexOf("USER_TASK") >= 0) { select.value = "USER_TASK"; }
}

function openConversation(conversationId) {
  return api("/api/conversations/" + conversationId).then(function (result) {
    const panel = document.getElementById("detail-panel");
    if (result.status !== 200) {
      showError(document.getElementById("list-error"), result);
      panel.hidden = true;
      return;
    }
    currentConversationId = conversationId;
    panel.hidden = false;
    const payload = result.payload;
    document.getElementById("detail-heading").textContent =
      payload.conversation.display_name + "  design_sha256: " + payload.design_sha256;
    renderRoles(payload.roles || []);
    currentConversationMessages = payload.messages || [];
    renderMessages(currentConversationMessages);
    renderSnapshots(payload.snapshots || []);
    document.getElementById("preview-result").hidden = true;
    return payload;
  });
}

// -- Context Preview --------------------------------------------------------

function numberFrom(id) {
  return Number(document.getElementById(id).value);
}

function renderPreview(payload) {
  const budget = payload.budget || {};
  document.getElementById("preview-budget").textContent =
    "total " + budget.total_tokens +
    " / 入力可能 " + budget.available_input_tokens +
    " / 選択 " + budget.selected_tokens +
    " / overflow " + budget.overflow_policy;
  document.getElementById("preview-decision").textContent =
    "decision_hash: " + payload.decision_hash + "  bundle_hash: " + payload.bundle_hash;

  const selected = document.getElementById("preview-selected");
  clear(selected);
  (payload.selected_fragments || []).forEach(function (fragment) {
    const tr = document.createElement("tr");
    tr.appendChild(text("td", fragment.fragment_id, "hash"));
    tr.appendChild(text("td", fragment.role));
    tr.appendChild(text("td", fragment.sequence_number));
    tr.appendChild(text("td", fragment.token_count));
    tr.appendChild(text("td", fragment.mandatory ? "必須" : ""));
    tr.appendChild(text("td", fragment.priority));
    selected.appendChild(tr);
  });

  const excluded = document.getElementById("preview-excluded");
  clear(excluded);
  (payload.excluded_fragments || []).forEach(function (item) {
    const tr = document.createElement("tr");
    tr.appendChild(text("td", item.fragment_id, "hash"));
    tr.appendChild(text("td", item.reason));
    excluded.appendChild(tr);
  });

  document.getElementById("preview-send-order").textContent =
    (payload.send_order || []).join(" → ");
  document.getElementById("preview-receipt").textContent =
    JSON.stringify(payload.receipt_projection, null, 2);
  document.getElementById("preview-result").hidden = false;
}

function submitPreview(event) {
  event.preventDefault();
  const errorNode = document.getElementById("preview-error");
  errorNode.textContent = "";
  if (!currentConversationId) {
    errorNode.textContent = "会話を選んでいない";
    return;
  }
  const snapshotId = document.getElementById("preview-snapshot").value;
  if (!snapshotId) {
    errorNode.textContent = "Snapshot がまだ無い";
    return;
  }
  if (!document.getElementById("preview-model").value.trim()) {
    errorNode.textContent = "確認用のモデルIDを選んでください。「コード編集で選んだAI・モデルを確認用に使う」でも設定できます。";
    return;
  }
  const body = {
    snapshot_id: snapshotId,
    mandatory_message_ids: [],
    budget: {
      total_tokens: numberFrom("preview-total"),
      reserved_output_tokens: numberFrom("preview-reserved-output"),
      reserved_tool_tokens: numberFrom("preview-reserved-tool"),
      safety_margin_tokens: numberFrom("preview-safety")
    },
    profile: {
      provider: document.getElementById("preview-provider").value,
      model: document.getElementById("preview-model").value,
      context_limit: numberFrom("preview-context-limit"),
      maximum_output_limit: numberFrom("preview-output-limit"),
      expires_at: document.getElementById("preview-expires").value,
      overheads: {
        system_message_overhead: 0,
        developer_message_overhead: 0,
        tool_definition_overhead: 0,
        per_message_overhead: 0,
        structured_output_overhead: 0,
        streaming_frame_overhead: 0,
        retry_fallback_reservation: 0
      }
    }
  };
  post("/api/conversations/" + currentConversationId + "/context-preview", body).then(
    function (result) {
      if (result.status !== 200) {
        document.getElementById("preview-result").hidden = true;
        showError(errorNode, result);
        return;
      }
      renderPreview(result.payload);
    }
  );
}

// -- 操作 -------------------------------------------------------------------

function submitMessage(event) {
  event.preventDefault();
  const errorNode = document.getElementById("composer-error");
  errorNode.textContent = "";
  if (!currentConversationId) {
    errorNode.textContent = "会話を選んでいない";
    return;
  }
  const body = {
    role: document.getElementById("role-select").value,
    text: document.getElementById("message-text").value
  };
  post("/api/conversations/" + currentConversationId + "/messages", body).then(function (result) {
    if (result.status !== 201) {
      // **成功表示を出さない。** 保存できていない。
      showError(errorNode, result);
      return;
    }
    document.getElementById("message-text").value = "";
    openConversation(currentConversationId);
    renderConversations();
  });
}

function buildSnapshot() {
  const errorNode = document.getElementById("snapshot-error");
  errorNode.textContent = "";
  if (!currentConversationId) {
    errorNode.textContent = "会話を選んでいない";
    return;
  }
  post("/api/conversations/" + currentConversationId + "/snapshots", {}).then(function (result) {
    if (result.status !== 201) {
      showError(errorNode, result);
      return;
    }
    requestedSnapshotId = result.payload.snapshot.snapshot_id;
    openConversation(currentConversationId);
    renderConversations();
  });
}

function createConversation() {
  post("/api/conversations", {}).then(function (result) {
    const errorNode = document.getElementById("list-error");
    if (result.status !== 201) {
      showError(errorNode, result);
      return;
    }
    errorNode.textContent = "";
    renderConversations().then(function () {
      openConversation(result.payload.conversation.conversation_id);
    });
  });
}

function trySend() {
  post("/api/chat/send", { conversation_id: currentConversationId }).then(function (result) {
    const node = document.getElementById("send-result");
    const error = result.payload && result.payload.error;
    node.textContent = error ? result.status + " " + error.reason : String(result.status);
  });
}

function defaultExpiry() {
  const oneHourLater = new Date(Date.now() + 3600 * 1000);
  return oneHourLater.toISOString().replace(/\.\d{3}Z$/, "Z");
}

document.getElementById("preview-expires").value = defaultExpiry();
document.getElementById("new-conversation").addEventListener("click", createConversation);
document.getElementById("composer").addEventListener("submit", submitMessage);
document.getElementById("preview-form").addEventListener("submit", submitPreview);
document.getElementById("build-snapshot").addEventListener("click", buildSnapshot);
document.getElementById("send-button").addEventListener("click", trySend);

renderProviders();
renderConversations();

// -- 用途別の希望 -----------------------------------------------------------
//
// **提案であって正本ではない。** Active Policy にしない。列挙順を順位にしない。

function renderRouteProfile() {
  return api("/api/route-profile").then(function (result) {
    const payload = result.payload;
    document.getElementById("route-profile-status").textContent = payload.status;
    document.getElementById("route-profile-note").textContent =
      "ACTIVE_ROUTE_POLICY: " + payload.active_route_policy +
      " / OWNER_DECISION_REQUIRED: " + payload.owner_decision_required;

    const rows = document.getElementById("route-profile-rows");
    clear(rows);
    (payload.usages || []).forEach(function (entry) {
      const tr = document.createElement("tr");
      [
        entry.usage,
        entry.preferred_provider_hint,
        "Canonical Provider Registry: " + entry.canonical_provider_registry,
        "Owner Proposal: " + entry.owner_proposal,
        "Execution: " + entry.execution
      ].forEach(function (value) {
        tr.appendChild(text("td", value));
      });
      rows.appendChild(tr);
    });

    const notes = document.getElementById("route-profile-not");
    clear(notes);
    (payload.what_this_is_not || []).forEach(function (line) {
      notes.appendChild(text("li", line));
    });
  });
}

// -- 具体値の入力支援 -------------------------------------------------------

const confirmations = {};

function renderValueInput() {
  return api("/api/provider-values").then(function (result) {
    const payload = result.payload;
    document.getElementById("value-derivation").textContent =
      payload.derivation.explanation;
    const measured = payload.measured;
    const writable = payload.save_allowed !== false && measured.inventory_available !== false;
    document.getElementById("value-save").disabled = !writable;
    document.getElementById("value-approve").disabled = !writable;
    document.getElementById("value-approve-label").hidden = !writable;
    document.getElementById("value-error").textContent = writable ? "" :
      "この旧設定の入力用紙は未配布です。実CLIの設定は上の「AI・モデル・推論レベルを変更」から選択・保存できます。";
    document.getElementById("value-summary").textContent =
      "入力欄 " + measured.fields_total +
      " / 入力済み " + measured.fields_present +
      " / 未入力 " + measured.fields_missing +
      " / 確認済み " + measured.fields_confirmed;

    const rows = document.getElementById("value-rows");
    clear(rows);
    (payload.steps || []).forEach(function (step) {
      const tr = document.createElement("tr");
      tr.appendChild(text("td", step.question + " " + step.name));
      tr.appendChild(text("td", step.purpose));
      tr.appendChild(text("td", step.current_state));
      // 入力例は形の説明だけである。値ではない。
      tr.appendChild(text("td", step.shape_hint));
      tr.appendChild(text("td", step.recommendation));

      const confirmCell = document.createElement("td");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.id = "confirm-" + step.field_id;
      confirmCell.appendChild(box);
      tr.appendChild(confirmCell);

      const valueCell = document.createElement("td");
      const input = document.createElement("input");
      input.type = "text";
      input.id = "value-" + step.field_id;
      input.placeholder = "未入力のまま保留してよい";
      valueCell.appendChild(input);
      tr.appendChild(valueCell);

      tr.appendChild(text("td", JSON.stringify(step.checks)));
      rows.appendChild(tr);

      box.addEventListener("change", function () {
        if (box.checked) {
          confirmations[step.field_id] = { confirmed: true, value: input.value };
        } else {
          delete confirmations[step.field_id];
        }
      });
      input.addEventListener("input", function () {
        if (confirmations[step.field_id]) {
          confirmations[step.field_id].value = input.value;
        }
      });
    });
  });
}

function saveProviderValues() {
  if (document.getElementById("value-save").disabled) { return; }
  const node = document.getElementById("value-error");
  node.textContent = "";
  const body = {
    confirmations: confirmations,
    approve_save: document.getElementById("value-approve").checked
  };
  post("/api/provider-values", body).then(function (result) {
    if (result.status !== 200) {
      // **成功表示を出さない。** 保存できていない。
      showError(node, result);
      return;
    }
    renderValueInput();
    renderProviders();
  });
}

document.getElementById("value-save").addEventListener("click", saveProviderValues);

// -- 実行モード ---------------------------------------------------------------

function renderExecutionModes() {
  // **実装状況は API から読む。** 画面側で「たぶん動く」を作らない。
  api("/api/execution-modes", {}).then(function (result) {
    if (result.status !== 200) {
      return;
    }
    const payload = result.payload;
    document.getElementById("execution-note").textContent = payload.note;
    const rows = document.getElementById("execution-rows");
    rows.textContent = "";
    payload.modes.forEach(function (mode) {
      const tr = document.createElement("tr");
      [
        mode.mode,
        mode.implemented ? "実装済み" : "未実装",
        mode.external_egress ? "外部へ出る" : "外部へ出ない",
        mode.note,
        mode.blocking.join(" / ") || "—"
      ].forEach(function (value) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      });
      rows.appendChild(tr);
    });
  });
}

// -- Mock の 1 往復 -----------------------------------------------------------

function runMockTurn(event) {
  event.preventDefault();
  const errorNode = document.getElementById("turn-error");
  const resultNode = document.getElementById("turn-result");
  const reasonsNode = document.getElementById("turn-reasons");
  const derivedNode = document.getElementById("turn-derived");
  errorNode.textContent = "";
  resultNode.textContent = "";
  reasonsNode.textContent = "";
  derivedNode.textContent = "";
  if (!currentConversationId) {
    errorNode.textContent = "会話を選んでいない";
    return;
  }
  // **Profile の Model が空なら送らない。** 既定値を入れない。
  if (!document.getElementById("preview-model").value) {
    errorNode.textContent =
      "Profile の Model が未設定である。model_id は Profile からしか引かないので送れない";
    return;
  }
  // **Metadata を送らない。** Application が導出する。
  const body = {
    conversation_id: currentConversationId,
    provider_id: document.getElementById("turn-provider").value,
    execution_mode: document.getElementById("turn-mode").value,
    text: document.getElementById("turn-text").value,
    mandatory_message_ids: [],
    budget: {
      total_tokens: numberFrom("preview-total"),
      reserved_output_tokens: numberFrom("preview-reserved-output"),
      reserved_tool_tokens: numberFrom("preview-reserved-tool"),
      safety_margin_tokens: numberFrom("preview-safety")
    },
    profile: {
      provider: document.getElementById("preview-provider").value,
      // **Model の出所は Profile だけである。** 送信側で別に受け取らない。
      model: document.getElementById("preview-model").value,
      context_limit: numberFrom("preview-context-limit"),
      maximum_output_limit: numberFrom("preview-output-limit"),
      expires_at: document.getElementById("preview-expires").value,
      overheads: {
        system_message_overhead: 0,
        developer_message_overhead: 0,
        tool_definition_overhead: 0,
        per_message_overhead: 0,
        structured_output_overhead: 0,
        streaming_frame_overhead: 0,
        retry_fallback_reservation: 0
      }
    }
  };
  post("/api/chat/mock-turn", body).then(function (result) {
    const payload = result.payload || {};
    if (payload.decision && payload.decision.reasons) {
      payload.decision.reasons.forEach(function (reason) {
        const li = document.createElement("li");
        li.textContent = reason;
        reasonsNode.appendChild(li);
      });
    }
    if (result.status !== 201) {
      showError(errorNode, result);
      openConversation(currentConversationId);
      return;
    }
    // **成功しても「送信済み」とは書かない。** 外部へは出ていない。
    resultNode.textContent =
      "Mock で 1 往復した。外部送出なし（network_used=" +
      payload.provider_response.network_used +
      "、adapter=" +
      payload.provider_response.adapter_version +
      "）";
    // **導出したものを、導出したと分かる形で見せる。**
    const derived = payload.derived_metadata;
    derivedNode.textContent =
      "導出: instruction_hash=" +
      derived.instruction_hash +
      "（" +
      derived.instruction_hash_source +
      "）／output_schema_hash=" +
      derived.output_schema_hash +
      "（" +
      derived.output_schema_source +
      "）／model_id=" +
      derived.model_id +
      "（" +
      derived.model_id_source +
      "）";
    document.getElementById("turn-text").value = "";
    openConversation(currentConversationId);
  });
}

document.getElementById("mock-turn-form").addEventListener("submit", runMockTurn);

renderExecutionModes();

renderRouteProfile();
renderValueInput();

// -- コード編集（CLI Workbench Preview）------------------------------------
//
// * 表示は必ず `textContent`。生成結果を HTML / Markdown / ANSI として解釈しない。
// * Session Token は Header だけで運ぶ。読取り系にも付ける（本文と差分は機微）。
// * Button の disabled は補助にすぎない。二重実行はサーバー側が 409 で断る。

let wbSession = null;
let wbReviewedConfirmation = null;
let wbTargets = [];
let wbPollTimer = null;
let wbQueue = null;
let wbQueueCreating = false;
let wbParentSessionId = null;
let wbSourceConversationId = null;
let wbHandoffRead = 0;
let wbSavedConversations = [];
let wbSavedChoiceRead = 0;

function wbApi(path, options) {
  const init = Object.assign({ headers: {} }, options || {});
  init.headers = Object.assign({}, init.headers);
  init.headers["X-Harness-Session"] = SESSION;
  if (init.method && init.method !== "GET") {
    init.headers["Content-Type"] = "application/json";
  }
  init.credentials = "omit";
  init.cache = "no-store";
  return fetch(path, init).then(function (response) {
    return response.json().then(function (payload) {
      return { status: response.status, payload: payload };
    });
  });
}

function wbPost(path, body) {
  return wbApi(path, { method: "POST", body: JSON.stringify(body || {}) });
}

function wbRow(label, value) {
  const tr = document.createElement("tr");
  tr.appendChild(text("th", label));
  tr.appendChild(text("td", value, "hash"));
  return tr;
}

function wbBusy(state) {
  return state === "SEND_PREPARED" || state === "SEND_ATTEMPTED";
}

function renderWorkbench() {
  return wbApi("/api/workbench").then(function (result) {
    const unavailable = document.getElementById("wb-unavailable");
    const body = document.getElementById("wb-body");
    const payload = result.payload || {};
    if (result.status !== 200 || !payload.available) {
      unavailable.hidden = false;
      unavailable.textContent =
        payload.reason ||
        (payload.error ? payload.error.reason : "AIワークスペースは利用できない");
      body.hidden = true;
      renderConnectionState([]);
      return;
    }
    unavailable.hidden = true;
    body.hidden = false;

    const workspace = payload.workspace || {};
    document.getElementById("wb-scope").textContent =
      "Workspace: " + workspace.workspace_label + "  ／  範囲: " + workspace.scope;
    const limits = workspace.limits || {};
    document.getElementById("wb-limits").textContent =
      "上限: 対象 " + limits.max_source_bytes + " Byte ／ 依頼 " +
      limits.max_instruction_bytes + " Byte ／ 送信 payload " +
      limits.max_request_bytes + " Byte ／ 出力 " + limits.max_stdout_bytes +
      " Byte ／ 制限時間 " + limits.timeout_seconds + " 秒" +
      "  ／  応答契約 " + workspace.output_contract;

    const failures = {};
    (payload.sessions || []).forEach(function (session) {
      if (session.failure && !failures[session.provider_id]) {
        failures[session.provider_id] = session.failure.class;
      }
    });

    const rows = document.getElementById("wb-provider-rows");
    clear(rows);
    const select = document.getElementById("wb-provider");
    const previous = select.value;
    clear(select);
    (payload.providers || []).forEach(function (provider) {
      const tr = document.createElement("tr");
      tr.appendChild(text("td", provider.display_name + "（" + provider.provider_id + "）"));
      tr.appendChild(text("td", provider.installed ? "導入済み" : "未導入"));
      tr.appendChild(text("td", provider.package_version || "UNVERIFIED"));
      tr.appendChild(
        text("td", provider.profile_verified ? "検証済み" : (provider.blocking_reason || "未検証"))
      );
      // **ログイン済みを推測しない。** 実接続が成功するまで UNVERIFIED のままにする。
      tr.appendChild(text("td", provider.login_state + "：" + provider.login_hint));
      tr.appendChild(text("td", failures[provider.provider_id] || "なし"));
      tr.appendChild(text("td", (provider.restriction_summary || []).join("\n")));
      tr.appendChild(text("td", (provider.residual_risks || []).join("\n")));
      rows.appendChild(tr);
      if (provider.installed && provider.profile_verified) {
        const option = document.createElement("option");
        option.value = provider.provider_id;
        option.textContent = provider.display_name;
        select.appendChild(option);
      }
    });
    if (previous) {
      select.value = previous;
    }
    renderWorkspacePurposes(payload.task_catalog || []);
    renderWorkbenchModels(payload.providers || []);
    renderChatGptConnection(payload.chatgpt);
    renderWorkbenchPreferences(payload.preferences || {});
    renderWorkbenchProviderCards(payload.providers || []);
    renderConnectionState(payload.providers || []);
    renderWorkbenchSessions(payload.sessions || []);
    const epoch = ++wbSavedChoiceRead;
    return Promise.all([renderWorkbenchTargets(), wbApi("/api/conversations").then(function (saved) {
      if (epoch !== wbSavedChoiceRead) { return; }
      if (saved.status !== 200) { return; }
      wbSavedConversations = saved.payload.conversations || [];
      renderWorkbenchConversationChoices(payload.sessions || []);
    })]);
  });
}

let wbModelProviders = [];
let wbModelProvider = null;

function workbenchModelId() {
  const value = document.getElementById("wb-model").value;
  return value === "__custom__" ? document.getElementById("wb-model-custom").value.trim() : value;
}

function renderWorkbenchModels(providers) {
  wbModelProviders = providers;
  const selected = document.getElementById("wb-provider").value;
  const model = document.getElementById("wb-model");
  const sameProvider = wbModelProvider === selected;
  const previous = sameProvider ? model.value : "";
  wbModelProvider = selected;
  clear(model);
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "モデルを選択してください";
  model.appendChild(placeholder);
  const provider = providers.find(function (item) { return item.provider_id === selected; });
  ((provider && provider.models) || []).forEach(function (id) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = (provider.model_labels && provider.model_labels[id]) || id;
    model.appendChild(option);
  });
  const custom = document.createElement("option");
  custom.value = "__custom__";
  custom.textContent = "モデル IDを直接入力…";
  if (selected !== "chatgpt") { model.appendChild(custom); }
  model.value = previous;
  if (!sameProvider) {
    document.getElementById("wb-model-custom").value = "";
    document.getElementById("wb-effort").value = "";
  }
  renderWorkbenchEffort();
}

function renderWorkbenchEffort() {
  const custom = document.getElementById("wb-model").value === "__custom__";
  document.getElementById("wb-model-custom").hidden = !custom;
  document.getElementById("wb-model-custom").required = custom;
  document.getElementById("wb-model-custom-label").hidden = !custom;
  const provider = wbModelProviders.find(function (item) { return item.provider_id === wbModelProvider; });
  const model = workbenchModelId();
  const levels = (provider && provider.model_efforts && provider.model_efforts[model]) || [];
  const effort = document.getElementById("wb-effort");
  const previous = effort.value;
  clear(effort);
  const labels = {low: "Low（軽め）", medium: "Medium（中程度）", high: "High（深く）", xhigh: "Extra high（さらに深く）", max: "Max（最大）"};
  [""].concat(levels).forEach(function (level) {
    const option = document.createElement("option");
    option.value = level;
    option.textContent = level ? labels[level] || level : "AIの既定値";
    effort.appendChild(option);
  });
  effort.value = levels.indexOf(previous) >= 0 ? previous : "";
  effort.disabled = levels.length === 0;
  document.getElementById("wb-model-note").textContent = levels.length ?
    "選択した推論レベルをCLIへ渡し、送信承認に記録します。高いレベルほど時間・利用量が増える場合があります。モデルの利用可否は契約に依存します。" :
    "このモデルで指定できる推論レベルは未確認のため、AIの既定値を使います。ChatGPTのモデル候補はログインしたアカウントから取得します。";
}

function renderWorkbenchTargets() {
  return wbApi("/api/workbench/targets").then(function (result) {
    if (result.status !== 200) {
      return;
    }
    wbTargets = result.payload.targets || [];
    const select = document.getElementById("wb-target");
    const previous = Array.from(select.selectedOptions).map(function (o) { return o.value; });
    clear(select);
    const denied = document.getElementById("wb-denied-rows");
    clear(denied);
    wbTargets.forEach(function (target) {
      if (target.eligible) {
        const option = document.createElement("option");
        option.value = target.relative_path;
        option.textContent = target.relative_path;
        select.appendChild(option);
      } else {
        const tr = document.createElement("tr");
        tr.appendChild(text("td", target.relative_path));
        tr.appendChild(text("td", target.reason));
        denied.appendChild(tr);
      }
    });
    Array.from(select.options).forEach(function (o) { o.selected = previous.indexOf(o.value) >= 0; });
    if (!previous.length && select.options.length) { select.options[0].selected = true; }
  });
}

function renderWorkbenchSessions(sessions) {
  document.getElementById("wb-session-count").textContent = "保存済み " + sessions.length + "件";
  document.getElementById("wb-history-empty").hidden = sessions.length !== 0;
  const rows = document.getElementById("wb-session-rows");
  clear(rows);
  renderWorkbenchConversationChoices(sessions);
  sessions.forEach(function (session) {
    const tr = document.createElement("tr");
    tr.appendChild(text("td", session.session_id.slice(0, 8), "hash"));
    const stateCell = document.createElement("td");
    stateCell.appendChild(text("span", session.state, workbenchStateClass(session.state)));
    tr.appendChild(stateCell);
    tr.appendChild(text("td", session.provider_id));
    tr.appendChild(text("td", session.model_id + " / " + (session.reasoning_effort || "CLI既定")));
    tr.appendChild(text("td", session.target || workspacePurposeLabel(session.task_kind)));
    tr.appendChild(text("td", session.created_at));
    tr.appendChild(text("td", session.failure ? session.failure.class : ""));
    const cell = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "開く";
    button.addEventListener("click", function () {
      openWorkbenchSession(session.session_id, true).then(function () {
        document.getElementById("wb-confirm-card").scrollIntoView({block: "start"});
      });
    });
    cell.appendChild(button);
    tr.appendChild(cell);
    rows.appendChild(tr);
  });
}

function renderWorkbenchQueue() {
  const panel = document.getElementById("wb-queue");
  panel.hidden = !wbQueue;
  if (!wbQueue) { return; }
  const list = document.getElementById("wb-queue-files");
  clear(list);
  wbQueue.paths.forEach(function (path, i) {
    const label = i < wbQueue.index ? "着手済み（履歴に保存）" : "未着手";
    list.appendChild(text("li", path + " — " + label));
  });
  const current = wbSession && wbSession.session_id === wbQueue.sessionId;
  const finished = current && ["APPLIED", "SEND_FAILED"].indexOf(wbSession.state) >= 0;
  const remaining = wbQueue.paths.length - wbQueue.index;
  document.getElementById("wb-queue-status").textContent =
    wbQueue.provider_id + " / " + wbQueue.model_id + "：未着手 " + remaining + " ファイル。" +
    (remaining ? "現在のファイルの適用完了後、次の送信内容を確認してください。確定した生成失敗の後も、ボタンで次へ進めます。" : "選択したファイルはすべて着手済みです。各結果は履歴で確認できます。");
  document.getElementById("wb-next-target").disabled = wbQueueCreating || wbMutationPending || !finished || !remaining;
  document.getElementById("wb-clear-queue").disabled = wbQueueCreating;
}

async function createQueuedWorkbenchSession() {
  if (!wbQueue || wbQueueCreating || wbQueue.index >= wbQueue.paths.length) { return; }
  const queue = wbQueue;
  const errorNode = document.getElementById("wb-form-error");
  errorNode.textContent = "";
  wbQueueCreating = true;
  document.getElementById("wb-create").disabled = true;
  renderWorkbenchQueue();
  try {
    // Planは前のファイルを適用した後の実体から作る。送信・適用はここでは行わない。
    const result = await wbPost("/api/workbench/sessions", {
      provider_id: queue.provider_id,
      model_id: queue.model_id,
      reasoning_effort: queue.reasoning_effort,
      relative_path: queue.paths[queue.index],
      instruction: queue.instruction,
      parent_session_id: queue.sessionId || queue.parent_session_id,
      conversation_id: queue.sessionId ? null : queue.conversation_id,
      history_selection: queue.history_selection,
      important_notes: queue.important_notes
    });
    if (result.status !== 201) { showError(errorNode, result); return; }
    queue.sessionId = result.payload.session_id;
    wbParentSessionId = queue.sessionId;
    wbSourceConversationId = null;
    queue.index += 1;
    await openWorkbenchSession(queue.sessionId);
    document.getElementById("wb-confirm-card").scrollIntoView({block: "start"});
  } catch (_) {
    errorNode.textContent = "依頼作成の応答を確認できません。自動送信はしていません。履歴を確認してください。";
  } finally {
    wbQueueCreating = false;
    document.getElementById("wb-create").disabled = false;
    renderWorkbenchQueue();
  }
}

function createWorkbenchSession(event) {
  event.preventDefault();
  if (wbQueueCreating) { return; }
  if (workspacePurpose() !== "file_edit") { return createTextSession(); }
  const errorNode = document.getElementById("wb-form-error");
  if (wbQueue && wbQueue.index < wbQueue.paths.length) {
    errorNode.textContent = "未着手の選択が残っています。次へ進むか、未着手の選択を解除してください。";
    return;
  }
  const paths = Array.from(document.getElementById("wb-target").selectedOptions).map(function (o) { return o.value; });
  if (!paths.length) { errorNode.textContent = "対象ファイルを選んでください。"; return; }
  wbQueue = {
    provider_id: document.getElementById("wb-provider").value,
    model_id: workbenchModelId(),
    reasoning_effort: document.getElementById("wb-effort").value || null,
    instruction: document.getElementById("wb-instruction").value,
    paths: paths, index: 0, sessionId: null,
    parent_session_id: wbParentSessionId,
    conversation_id: wbSourceConversationId,
    history_selection: workbenchHistorySelection(),
    important_notes: document.getElementById("wb-important-notes").value
  };
  return createQueuedWorkbenchSession();
}

let wbSelectedSessionId = null;
let wbSessionRead = 0;
let wbMutationPending = false;

function openWorkbenchSession(sessionId, continueFrom) {
  wbSelectedSessionId = sessionId;
  wbReviewedConfirmation = null;
  document.getElementById("wb-payload").textContent = "";
  document.getElementById("wb-transparency").textContent = "";
  document.getElementById("wb-approve-send").disabled = true;
  wbArtifact = null;
  document.getElementById("wb-result-card").hidden = true;
  const read = ++wbSessionRead;
  // 会話の取得・表示が完了する前に次のファイルを操作させない。
  document.getElementById("wb-next-target").disabled = true;
  if (wbPollTimer !== null) {
    window.clearTimeout(wbPollTimer);
    wbPollTimer = null;
  }
  return wbApi("/api/workbench/sessions/" + sessionId).then(function (result) {
    if (read !== wbSessionRead || sessionId !== wbSelectedSessionId) { return; }
    if (result.status !== 200) {
      showError(document.getElementById("wb-form-error"), result);
      return;
    }
    wbSession = result.payload;
    if (continueFrom) {
      wbParentSessionId = sessionId;
      wbSourceConversationId = null;
      chooseWorkspacePurpose(result.payload.task_kind || "file_edit");
    }
    return renderWorkbenchSession().then(function () {
      if (read === wbSessionRead && sessionId === wbSelectedSessionId) {
        renderWorkbenchQueue();
      }
    });
  }).catch(function () {
    if (read !== wbSessionRead) { return; }
    document.getElementById("wb-send-status").textContent = "状態を取得できません。操作を繰り返さず、状態を確認してください。";
    document.getElementById("wb-approve-send").disabled = true;
    document.getElementById("wb-approve-apply").disabled = true;
    document.getElementById("wb-recover").disabled = true;
  });
}

async function renderWorkbenchSession() {
  const session = wbSession;
  if (session && session.session_id === wbParentSessionId) {
    await refreshWorkbenchConversation();
  }
  // 履歴を待つ間に別Sessionへ移ったなら、古い確認内容で上書きしない。
  if (session !== wbSession || (session && session.session_id !== wbSelectedSessionId)) {
    return;
  }
  const confirm = document.getElementById("wb-confirm");
  const diff = document.getElementById("wb-diff");
  if (!session) {
    confirm.hidden = true;
    diff.hidden = true;
    document.getElementById("wb-result-card").hidden = true;
    return Promise.resolve();
  }
  const rows = document.getElementById("wb-confirm-rows");
  clear(rows);
  rows.appendChild(wbRow("Session", session.session_id));
  rows.appendChild(wbRow("状態", session.state));
  rows.appendChild(wbRow("Provider / モデル", session.provider_id + " / " + session.model_id));
  rows.appendChild(wbRow("推論レベル", session.reasoning_effort || "AIの既定値"));
  rows.appendChild(wbRow("用途", workspacePurposeLabel(session.task_kind || "file_edit")));
  if (session.can_apply !== false) {
    rows.appendChild(wbRow("対象ファイル", session.target));
    rows.appendChild(wbRow("対象の変更前 Hash", session.before_hash));
  } else {
    rows.appendChild(wbRow("成果物の形式", session.output_format === "markdown" ? "Markdown" : "文章"));
  }
  rows.appendChild(wbRow("依頼", session.instruction));
  if (session.handoff) {
    rows.appendChild(wbRow(
      "引き継ぐ会話", "保存メッセージ " + session.handoff.local_message_count +
      "件 / 過去の依頼 " + session.handoff.turn_count + "件" +
      (session.handoff.selection ? "（選択範囲は送信内容で確認）" : "（省略なし）")
    ));
    rows.appendChild(wbRow("会話の内容 Hash", session.handoff.history_hash));
  }
  rows.appendChild(wbRow("送信 payload Hash", session.request_payload_hash));
  rows.appendChild(wbRow("実行体・設定の同一性 Hash", session.runtime_hash));
  rows.appendChild(wbRow("送信 Plan Hash", session.execution_plan_hash));
  rows.appendChild(wbRow("承認の期限", session.expires_at));
  const commercial = session.commercial_disclosure || {};
  rows.appendChild(
    wbRow(
      "通信と利用枠",
      "外部通信あり（network_used=" + commercial.network_used +
        "）／課金モード " + commercial.billing_mode +
        "／利用枠 " + commercial.usage_quota_state +
        "／資格 " + commercial.entitlement_state
    )
  );
  if (session.send_observation) {
    rows.appendChild(
      wbRow(
        "送信の観測",
        session.send_observation.transport === "HTTPS_RESPONSES_SSE" ?
          session.send_observation.outcome + "（HTTP " + session.send_observation.http_status +
          "、終端 " + (session.send_observation.terminal_event || "未確認") +
          "、受信 " + session.send_observation.response_bytes + " Bytes）" :
        session.send_observation.outcome +
          "（exit=" + session.send_observation.exit_code +
          "、stdout=" + session.send_observation.stdout_bytes +
          " Byte、stderr=" + session.send_observation.stderr_bytes +
          " Byte、診断 " + session.send_observation.diagnostic_id + "）" +
          " 分類: " + (session.send_observation.stderr_classifications || []).join(", ")
      )
    );
  }
  if (session.failure) {
    rows.appendChild(wbRow("失敗", session.failure.class + "：" + session.failure.detail));
  }

  const sendButton = document.getElementById("wb-approve-send");
  const status = document.getElementById("wb-send-status");
  confirm.hidden = false;
  const running = session.job && session.job.running_session_id;
  sendButton.disabled = !workbenchConfirmationMatches(session) || wbMutationPending ||
    ["DRAFTED", "SEND_APPROVED", "SEND_PREPARED"].indexOf(session.state) < 0 || !!running;
  sendButton.textContent = session.state === "SEND_PREPARED" ? "準備済みの未送信依頼を送信する" :
    session.state === "SEND_APPROVED" ? "承認済みの内容を送信する" : "この内容を送信して生成";
  const recovery = document.getElementById("wb-recover");
  recovery.hidden = ["APPLY_PREPARED", "APPLY_ATTEMPTED"].indexOf(session.state) < 0;
  recovery.disabled = wbMutationPending;
  document.getElementById("wb-recovery-status").textContent = recovery.hidden ? "" :
    "適用が中断しています。実ファイルと記録を照合し、一致する場合だけ完了を記録します。不一致では停止し、再適用しません。";
  if (wbBusy(session.state) && running === session.session_id) {
    status.textContent =
      "生成中。停止しても送信の取消しや利用枠の返却は意味しない。" +
      "再読込みや別タブから同じ送信をもう一度起こすことはできない。";
    schedulePoll(session.session_id);
  } else if (session.state === "SEND_PREPARED") {
    status.textContent = "未送信の準備状態です。送信内容と期限を確認し、ボタンで明示的に続行できます。";
  } else if (session.state === "SEND_ATTEMPTED") {
    status.textContent = "送信開始の記録がありますが、稼働中のジョブを確認できません。再送せず、記録の調査が必要です。";
  } else if (session.state === "SEND_APPROVED") {
    status.textContent = "送信承認は保存済みです。同じ内容を続行できます。期限や対象が変わった場合はサーバーが拒否します。";
  } else if (session.state === "SEND_UNKNOWN") {
    status.textContent =
      "結果不明。送信済みかどうか、利用枠を消費したかどうかは判定できない。自動再送はしない。";
  } else if (session.state === "SEND_FAILED") {
    status.textContent = "生成は失敗した。ファイルは変更していない。";
  } else {
    status.textContent = "";
  }

  renderWorkbenchProgress(session);
  document.getElementById("wb-diff-card").hidden = session.can_apply === false;
  document.getElementById("wb-result-card").hidden = true;
  if (session.can_apply === false) {
    diff.hidden = true;
    document.getElementById("wb-approve-apply").disabled = true;
    return loadConfirmation(session.session_id).then(function () {
      return session.state === "PROPOSAL_READY" ? loadWorkspaceResult(session.session_id) : null;
    });
  }
  const hasProposal =
    session.state === "PROPOSAL_READY" ||
    session.state === "APPLY_APPROVED" ||
    session.state === "APPLIED" ||
    session.state === "APPLY_UNKNOWN";
  if (!hasProposal) {
    diff.hidden = true;
    document.getElementById("wb-diff-empty").hidden = false;
    return loadConfirmation(session.session_id);
  }
  return loadConfirmation(session.session_id).then(function () {
    return loadWorkbenchDiff(session.session_id);
  });
}

function loadConfirmation(sessionId) {
  return wbApi("/api/workbench/sessions/" + sessionId + "/confirmation").then(function (result) {
    if (!wbSession || wbSelectedSessionId !== sessionId) { return; }
    const payload = document.getElementById("wb-payload");
    const note = document.getElementById("wb-transparency");
    if (result.status !== 200 || !wbSession ||
        result.payload.execution_plan_hash !== wbSession.execution_plan_hash ||
        result.payload.request_payload_hash !== wbSession.request_payload_hash) {
      wbReviewedConfirmation = null;
      document.getElementById("wb-approve-send").disabled = true;
      showError(document.getElementById("wb-send-error"), result);
      payload.textContent = "";
      note.textContent = "";
      document.getElementById("wb-confirm-empty").hidden = false;
      return;
    }
    // **生成物も payload も textContent で置く。** HTML としても Markdown としても解釈しない。
    document.getElementById("wb-confirm-empty").hidden = true;
    payload.textContent = result.payload.request_payload_text;
    wbReviewedConfirmation = {
      session_id: sessionId, execution_plan_hash: result.payload.execution_plan_hash,
      request_payload_hash: result.payload.request_payload_hash
    };
    document.getElementById("wb-approve-send").disabled = wbMutationPending ||
      !workbenchConfirmationMatches(wbSession) ||
      ["DRAFTED", "SEND_APPROVED", "SEND_PREPARED"].indexOf(wbSession.state) < 0 ||
      !!(wbSession.job && wbSession.job.running_session_id);
    note.textContent = result.payload.transparency_note;
    const budget = result.payload.context_budget;
    const receipt = result.payload.handoff && result.payload.handoff.selection_receipt;
    document.getElementById("wb-context-budget").textContent =
      "送信容量 " + budget.request_bytes.toLocaleString() + " / " +
      budget.maximum_request_bytes.toLocaleString() + " Bytes。トークン数・モデル上限は未計測。" +
      (receipt ? " 履歴の選択: " + receipt.selected_turns + "/" + receipt.original_turns +
       "依頼、" + receipt.selected_messages + "/" + receipt.original_messages + "メッセージ。" : "");
    if (receipt) { note.textContent += " " + receipt.note; }
  });
}

function loadWorkbenchDiff(sessionId) {
  return wbApi("/api/workbench/sessions/" + sessionId + "/diff").then(function (result) {
    if (!wbSession || wbSelectedSessionId !== sessionId) { return; }
    const diff = document.getElementById("wb-diff");
    if (result.status !== 200) {
      diff.hidden = true;
      document.getElementById("wb-diff-empty").hidden = false;
      return;
    }
    const payload = result.payload;
    diff.hidden = false;
    document.getElementById("wb-diff-empty").hidden = true;
    document.getElementById("wb-diff-meta").textContent =
      "対象 " + payload.target +
      "／変更前 " + payload.before_hash +
      "／変更後 " + payload.after_hash +
      (payload.target_changed_outside ? "  ※対象ファイルが外部で変更されている" : "");
    document.getElementById("wb-before").textContent = payload.before_text;
    document.getElementById("wb-after").textContent = payload.after_text;
    document.getElementById("wb-unified").textContent = payload.unified_diff;
    const button = document.getElementById("wb-approve-apply");
    button.disabled = wbMutationPending || payload.target_changed_outside ||
      ["PROPOSAL_READY", "APPLY_APPROVED"].indexOf(wbSession.state) < 0;
    button.textContent = wbSession.state === "APPLY_APPROVED" ?
      "承認済みの差分を適用する" : "この差分を承認して適用";
    const applied = document.getElementById("wb-apply-result");
    if (wbSession.apply_result) {
      applied.textContent =
        "適用した。観測 Hash " + wbSession.apply_result.observed_hash +
        "／対象一致 " + wbSession.apply_result.target_matches +
        "／対象以外は不変 " + wbSession.apply_result.no_other_file_changed +
        "／Receipt " + (wbSession.effect_receipt_hash || "") +
        "。" + wbSession.apply_result.quality_assurance;
    } else {
      applied.textContent = "";
    }
  });
}

function schedulePoll(sessionId) {
  if (wbPollTimer !== null) {
    return;
  }
  wbPollTimer = window.setTimeout(function () {
    wbPollTimer = null;
    if (wbSelectedSessionId !== sessionId) { return; }
    openWorkbenchSession(sessionId).then(function () {
      if (wbSession && !wbBusy(wbSession.state)) {
        renderWorkbench();
      }
    });
  }, 1500);
}

// 失敗応答や通信切断ではPOSTを自動再試行しない。GETで永続状態を確認する。
async function workbenchMutation(errorId, operation) {
  if (wbMutationPending || !wbSession || wbSession.session_id !== wbSelectedSessionId) { return; }
  const session = wbSession;
  const errorNode = document.getElementById(errorId);
  errorNode.textContent = "";
  wbMutationPending = true;
  document.getElementById("wb-approve-send").disabled = true;
  document.getElementById("wb-approve-apply").disabled = true;
  document.getElementById("wb-recover").disabled = true;
  try {
    const result = await operation(session);
    if (wbSelectedSessionId === session.session_id && result.status >= 400) {
      showError(errorNode, result);
    }
  } catch (_) {
    if (wbSelectedSessionId === session.session_id) {
      errorNode.textContent = "応答を確認できませんでした。自動再送はせず、保存された状態を確認します。";
    }
  } finally {
    wbMutationPending = false;
    renderWorkbenchQueue();
    if (wbSelectedSessionId === session.session_id) {
      await openWorkbenchSession(session.session_id);
    } else if (wbSelectedSessionId) {
      await openWorkbenchSession(wbSelectedSessionId);
    }
    await renderWorkbench();
  }
}

function workbenchConfirmationMatches(session) {
  return !!(session && wbReviewedConfirmation &&
    wbReviewedConfirmation.session_id === session.session_id &&
    wbReviewedConfirmation.execution_plan_hash === session.execution_plan_hash &&
    wbReviewedConfirmation.request_payload_hash === session.request_payload_hash);
}
function approveAndSend() {
  if (!workbenchConfirmationMatches(wbSession)) {
    document.getElementById("wb-send-error").textContent = "現在の依頼の送信内容を取得してから確認・承認してください。";
    return Promise.resolve();
  }
  return workbenchMutation("wb-send-error", async function (session) {
    const base = "/api/workbench/sessions/" + session.session_id;
    const body = {execution_plan_hash: session.execution_plan_hash};
    if (session.state === "DRAFTED") {
      const approval = await wbPost(base + "/approve-send", body);
      if (approval.status !== 200) { return approval; }
    } else if (session.state === "SEND_PREPARED") {
      return wbPost(base + "/resume-send", {});
    } else if (session.state !== "SEND_APPROVED") {
      return {status: 409, payload: {error: {reason: "この状態からは送信できません"}}};
    }
    return wbPost(base + "/send", body);
  });
}

function approveAndApply() {
  return workbenchMutation("wb-apply-error", async function (session) {
    const base = "/api/workbench/sessions/" + session.session_id;
    if (session.state === "PROPOSAL_READY") {
      const approval = await wbPost(base + "/approve-apply", {
        apply_execution_plan_hash: session.apply_execution_plan_hash,
        proposal_hash: session.proposal_hash
      });
      if (approval.status !== 200) { return approval; }
    } else if (session.state !== "APPLY_APPROVED") {
      return {status: 409, payload: {error: {reason: "この状態からは適用できません"}}};
    }
    return wbPost(base + "/apply", {apply_execution_plan_hash: session.apply_execution_plan_hash});
  });
}

function recoverWorkbenchApply() {
  return workbenchMutation("wb-send-error", function (session) {
    return wbPost("/api/workbench/sessions/" + session.session_id + "/recover", {});
  });
}

document.getElementById("wb-refresh-session").addEventListener("click", function () {
  if (wbSelectedSessionId) { openWorkbenchSession(wbSelectedSessionId); }
});
document.getElementById("wb-recover").addEventListener("click", recoverWorkbenchApply);

document.getElementById("wb-model").addEventListener("change", renderWorkbenchEffort);
document.getElementById("wb-model-custom").addEventListener("input", renderWorkbenchEffort);

document.getElementById("wb-form").addEventListener("submit", createWorkbenchSession);
document.getElementById("wb-approve-send").addEventListener("click", approveAndSend);
document.getElementById("wb-approve-apply").addEventListener("click", approveAndApply);
document.getElementById("wb-provider").addEventListener("change", function () {
  renderWorkbench();
});

renderWorkbench();


document.getElementById("wb-next-target").addEventListener("click", function () {
  if (!wbQueue || !wbSession || wbSession.session_id !== wbQueue.sessionId ||
      ["APPLIED", "SEND_FAILED"].indexOf(wbSession.state) < 0 || wbMutationPending) { return; }
  createQueuedWorkbenchSession();
});
document.getElementById("wb-clear-queue").addEventListener("click", function () {
  if (wbQueueCreating) { return; }
  // 作成済みSession・承認・送信には触れず、ブラウザー内の未着手リストだけを解除。
  wbQueue = null;
  document.getElementById("wb-queue").hidden = true;
});


// -- 明示的な運用承認。自動再送・整理後の自動再開はしない。 ----------------------
let opsReview = null;
let opsBusy = false;
const opsNames = ["reconcile", "resume"];
// 確認内容を無効にしたら、画面に古い「検査通過」やHashを残さない。承認は必ず再確認から。
function opsInvalidate(message) {
  opsNames.forEach(function (name) {
    document.getElementById("ops-" + name + "-summary").textContent = message;
    document.getElementById("ops-" + name + "-review").textContent = "";
    document.getElementById("ops-" + name + "-approve").checked = false;
  });
}
// 押したボタンは処理中にdisabledとなりfocusを失う。運用Panel内で操作していた場合だけ戻す。
function opsHadFocus() {
  const active = document.activeElement;
  return !!active && document.getElementById("operations-panel").contains(active);
}
function opsRestoreFocus(hadFocus) {
  const active = document.activeElement;
  // フォーカス可能なmainを追加した画面では、disabled後の退避先がbodyではなくmainになる。
  const fallback = active === document.body || active === document.getElementById("main-content");
  if (hadFocus && (!active || fallback || active.disabled)) {
    document.getElementById("ops-refresh").focus();
  }
}
function opsControls() {
  opsNames.forEach(function (name) {
    const checkbox = document.getElementById("ops-" + name + "-approve");
    const enabled = !opsBusy && opsReview && opsReview.write_enabled && opsReview[name].eligible;
    checkbox.disabled = !enabled;
    document.getElementById("ops-" + name).disabled = !enabled || !checkbox.checked;
  });
  document.getElementById("ops-refresh").disabled = opsBusy;
}
async function refreshOperations() {
  if (opsBusy) { return; }
  const hadFocus = opsHadFocus();
  opsReview = null;
  opsBusy = true;
  opsInvalidate("確認しています…");
  opsControls();
  document.getElementById("ops-error").textContent = "";
  document.getElementById("ops-status").textContent = "確認しています…";
  try {
    const result = await api("/api/operations", {headers: {"X-Harness-Session": SESSION}});
    if (result.status !== 200) {
      showError(document.getElementById("ops-error"), result);
      document.getElementById("ops-status").textContent = "状態を確認できません。操作は無効です。";
      opsInvalidate("状態を確認できないため操作できません。");
      return;
    }
    opsReview = result.payload;
    document.getElementById("ops-status").textContent = "受付状態: " + opsReview.resume.snapshot.mode +
      (opsReview.write_enabled ? " ／ 運用操作が有効です" : " ／ 参照専用（起動時の --operator-auth-session 指定が必要です）");
    document.getElementById("ops-start-help").hidden = opsReview.write_enabled;
    const state = opsReview.resume.snapshot;
    document.getElementById("ops-guidance").textContent = state.mode === "OPEN" && state.ledger_valid ?
      "通常運転中です。新しいコード編集を開始できます。整理や受付再開は、処理が停止した場合に使うため、現在は操作不要です。" :
      "新しい処理の受付を止めているか、安全確認が必要な状態です。下の理由と対処を確認し、整理と再開を順に別々に承認してください。";
    opsNames.forEach(function (name) {
      const review = opsReview[name];
      document.getElementById("ops-" + name + "-summary").textContent = review.eligible ?
        "検査通過。確認内容を読んでから承認してください。" :
        "操作不可: " + review.blockers.map(operationReason).join(" / ");
      document.getElementById("ops-" + name + "-review").textContent = JSON.stringify(review, null, 2);
    });
  } catch (error) {
    document.getElementById("ops-status").textContent = "状態は未確認です。再確認してください。";
    document.getElementById("ops-error").textContent = String(error);
    opsInvalidate("状態を確認できないため操作できません。");
  } finally { opsBusy = false; opsControls(); opsRestoreFocus(hadFocus); }
}
async function decideOperation(name) {
  if (opsBusy || !opsReview || !opsReview.write_enabled || !opsReview[name].eligible ||
      !document.getElementById("ops-" + name + "-approve").checked) { return; }
  const hadFocus = opsHadFocus();
  const hash = opsReview[name].review_hash;
  opsReview = null;
  opsBusy = true;
  opsInvalidate("この確認内容は使用済みです。「現在の状態を確認」で最新の確認内容を読んでください。");
  opsControls();
  document.getElementById("ops-error").textContent = "";
  try {
    const result = await post("/api/operations/" + name, {review_hash: hash, approve: true});
    document.getElementById("ops-result").textContent = JSON.stringify(result.payload, null, 2);
    showError(document.getElementById("ops-error"), result);
    document.getElementById("ops-status").textContent = result.status === 200 ?
      (name === "reconcile" ? "整理を記録しました。受付は停止中です。現在の状態を再確認してください。" : "受付を再開しました。") :
      "操作が完了したとは判断できません。現在の状態と監査Receiptを確認してください。";
  } catch (error) {
    document.getElementById("ops-status").textContent = "結果不明。再送せず、現在の状態と監査Receiptを確認してください。";
    document.getElementById("ops-error").textContent = String(error);
  } finally { opsBusy = false; opsControls(); opsRestoreFocus(hadFocus); }
}
opsNames.forEach(function (name) {
  document.getElementById("ops-" + name + "-approve").addEventListener("change", opsControls);
  document.getElementById("ops-" + name).addEventListener("click", function () { decideOperation(name); });
});
document.getElementById("ops-refresh").addEventListener("click", refreshOperations);
refreshOperations();


// Form preferences are not an approval grant and never trigger generation.
let wbPreferenceVersion = 0;
let wbPreferenceInitialized = false;
function renderWorkbenchPreferences(preferences) {
  wbPreferenceVersion = preferences.version || 0;
  const status = document.getElementById("wb-preference-status");
  document.getElementById("wb-preference-save").disabled = !preferences.available;
  if (wbPreferenceInitialized) { return; }
  wbPreferenceInitialized = true;
  const saved = preferences.selection;
  if (!preferences.configured || !saved) {
    status.textContent = "未設定です。モデルと推論レベルを選択して保存できます。";
    return;
  }
  const provider = document.getElementById("wb-provider");
  provider.value = saved.provider_id;
  renderWorkbenchModels(wbModelProviders);
  if (provider.value !== saved.provider_id) {
    status.textContent = "保存済みのProviderは現在利用できません。別のProviderを明示的に選択してください。";
    return;
  }
  const model = document.getElementById("wb-model");
  model.value = saved.model_id;
  if (model.value !== saved.model_id) {
    model.value = "__custom__";
    document.getElementById("wb-model-custom").value = saved.model_id;
  }
  renderWorkbenchEffort();
  const effort = document.getElementById("wb-effort");
  effort.value = saved.reasoning_effort || "";
  if (effort.value !== (saved.reasoning_effort || "")) {
    status.textContent = "保存済みの推論レベルは現在未対応です。モデルと推論レベルを選び直してください。";
    model.value = "";
    return;
  }
  status.textContent = "保存した設定を読み込みました。送信・適用は承認されていません。";
}

document.getElementById("wb-preference-save").addEventListener("click", function () {
  const status = document.getElementById("wb-preference-status");
  const approval = document.getElementById("wb-preference-approve");
  if (!approval.checked) { status.textContent = "設定の保存を確認してください。"; return; }
  const body = {provider_id: document.getElementById("wb-provider").value,
    model_id: workbenchModelId(), reasoning_effort: document.getElementById("wb-effort").value || null,
    expected_version: wbPreferenceVersion, approved: true};
  approval.checked = false;
  wbPost("/api/workbench/preferences", body).then(function (result) {
    if (result.status !== 200) {
      status.textContent = result.payload.error ? result.payload.error.reason : "保存できませんでした。再読込みして確認してください。";
      return;
    }
    wbPreferenceVersion = result.payload.version;
    status.textContent = "設定を保存しました。CLIの起動・外部送信は行っていません。";
  }).catch(function () { status.textContent = "保存結果を確認できません。再読込みして確認してください。"; });
});
["wb-provider", "wb-model", "wb-model-custom", "wb-effort"].forEach(function (id) {
  document.getElementById(id).addEventListener("change", function () {
    document.getElementById("wb-preference-approve").checked = false;
  });
});


// -- Workspace の表示（状態と承認の判定は既存のService/操作経路が所有する）------

function workbenchStateClass(state) {
  const base = "history-state";
  if (state === "APPLIED") { return base + " state-complete"; }
  if (["SEND_UNKNOWN", "SEND_FAILED", "APPLY_UNKNOWN"].indexOf(state) >= 0) {
    return base + " state-attention";
  }
  if (["PROPOSAL_READY", "APPLY_APPROVED"].indexOf(state) >= 0) {
    return base + " state-ready";
  }
  return base;
}

function renderWorkbenchProviderCards(providers) {
  const cards = document.getElementById("wb-provider-cards");
  const selected = document.getElementById("wb-provider").value;
  const focused = document.activeElement && cards.contains(document.activeElement);
  clear(cards);
  const names = {claude: "Claude", codex: "Codex", gemini: "Gemini"};
  const glyphs = {claude: "✳", codex: "{ }", gemini: "✦"};
  providers.forEach(function (provider) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "provider-card";
    if (Object.prototype.hasOwnProperty.call(names, provider.provider_id)) {
      button.classList.add("provider-" + provider.provider_id);
    }
    button.setAttribute("aria-pressed", String(selected === provider.provider_id));
    button.disabled = !provider.installed || !provider.profile_verified;
    const known = Object.prototype.hasOwnProperty.call(names, provider.provider_id);
    const label = known ? names[provider.provider_id] : provider.display_name;
    button.setAttribute("aria-label", label + " CLIを選択");
    const glyph = text("span", known ? glyphs[provider.provider_id] : "AI", "provider-glyph");
    glyph.setAttribute("aria-hidden", "true");
    button.appendChild(glyph);
    const copy = document.createElement("span");
    copy.className = "provider-card-copy";
    copy.appendChild(text("strong", label));
    // 設定の確認を認証済みと表示しない。資格情報は読まず、APIの認証状態をそのまま出す。
    copy.appendChild(text("small", "認証: " + (provider.login_state === "UNVERIFIED" ? "未確認" : provider.login_state)));
    button.appendChild(copy);
    const state = !provider.installed ? "未導入" : (provider.profile_verified ? "設定確認済み" : "設定未検証");
    button.appendChild(text("span", state, "provider-state"));
    button.addEventListener("click", function () {
      const select = document.getElementById("wb-provider");
      select.value = provider.provider_id;
      // 既存の選択経路へ渡すだけ。モデルの創作・設定の保存・送信は行わない。
      select.dispatchEvent(new Event("change"));
    });
    cards.appendChild(button);
    if (focused && selected === provider.provider_id) { button.focus({preventScroll: true}); }
  });
  if (!wbSession) {
    const status = document.getElementById("wb-workflow-status");
    status.textContent = selected ? "設定を選んで依頼を作成" : "使えるCLI設定がありません";
    status.classList.toggle("is-attention", !selected);
  }
}

function renderWorkbenchProgress(session) {
  const stages = {
    DRAFTED: [2, "送信内容の確認待ち"],
    SEND_APPROVED: [3, "送信承認を保存済み"],
    SEND_PREPARED: [3, "送信の準備状態"],
    SEND_ATTEMPTED: [3, "生成状態を確認中"],
    SEND_UNKNOWN: [3, "生成結果が不明・再送停止"],
    SEND_FAILED: [3, "生成に失敗・内容を確認"],
    PROPOSAL_READY: [4, "差分の確認待ち"],
    APPLY_APPROVED: [4, "適用承認を保存済み"],
    APPLY_PREPARED: [4, "適用の準備状態"],
    APPLY_ATTEMPTED: [4, "適用状態を確認中"],
    APPLY_FAILED: [4, "適用に失敗・内容を確認"],
    APPLY_UNKNOWN: [4, "適用結果が不明・照合が必要"],
    APPLIED: [4, "適用完了"]
  };
  const known = Object.prototype.hasOwnProperty.call(stages, session.state);
  const projection = session.can_apply === false && session.state === "PROPOSAL_READY" ? [4, "成果物を保存済み"] : known ? stages[session.state] : [0, "状態を確認できません"];
  const step = projection[0];
  document.querySelectorAll("[data-workflow-step]").forEach(function (item) {
    const number = Number(item.dataset.workflowStep);
    item.classList.toggle("is-current", number === step);
    item.classList.toggle("is-complete", known && (number < step || (number === step && session.state === "APPLIED")));
    if (number === step) { item.setAttribute("aria-current", "step"); }
    else { item.removeAttribute("aria-current"); }
  });
  const status = document.getElementById("wb-workflow-status");
  status.textContent = projection[1];
  status.classList.toggle("is-attention", !known || ["SEND_UNKNOWN", "SEND_FAILED", "APPLY_UNKNOWN", "APPLY_FAILED"].indexOf(session.state) >= 0);
}

function renderWorkspaceNavigation() {
  const selected = window.location.hash.slice(1) || "workbench-panel";
  document.querySelectorAll("[data-nav-target]").forEach(function (link) {
    if (link.dataset.navTarget === selected) { link.setAttribute("aria-current", "page"); }
    else { link.removeAttribute("aria-current"); }
  });
}
window.addEventListener("hashchange", renderWorkspaceNavigation);
renderWorkspaceNavigation();


// -- Explain the existing local contracts without enabling an unimplemented adapter. ---
function operationReason(code) {
  const reasons = {
    REQUIRES_DRAINING: "すでに受付中なので再開は不要です。停止中のときに使用します",
    STOP_INTAKE_BEFORE_RECONCILIATION: "受付中に予約を整理する必要はありません。保守で受付を停止した後に使用します",
    NOTHING_TO_RECONCILE: "整理する残存処理はありません",
    RESERVATION_OWNER_ACTIVE_OR_UNKNOWN: "処理が実行中、または終了を確認できません。処理の終了後に状態を再確認してください",
    ACTIVE_OR_UNRECONCILED_RESERVATIONS: "未確定の予約があります。終了を確認できた処理を整理してから再確認してください",
    UNSETTLED_JOURNALS_OR_APPROVALS: "送信・適用の結果が未確定です。コード編集の履歴から結果とReceiptを確認してください。自動で再送しません",
    AUDIT_ARTIFACTS_UNVERIFIED: "監査資料の検証が通りません。保存先とBackupを確認してください",
    LEDGER_VERIFICATION_FAILED: "保存履歴の整合性を確認できません。Backupと照合し、修復前に再開しないでください",
    LEGACY_RESTORE_WITHOUT_SOURCE_BINDING: "古い復元記録の元データを照合できません。対応するBackupで確認してください"
  };
  return (reasons[code] || "安全条件を確認できません。操作記録を確認してください") + "（" + code + "）";
}

function renderConnectionState(providers) {
  const root = document.getElementById("connection-overview");
  clear(root);
  providers.forEach(function (provider) {
    const card = document.createElement("article");
    card.className = "connection-card";
    card.appendChild(text("h3", provider.display_name));
    card.appendChild(text("p", provider.profile_verified ? "CLI設定・操作範囲の検証済み" :
      "現在は使用不可: " + (provider.blocking_reason || "CLIの導入と起動設定を確認してください")));
    card.appendChild(text("p", "CLI版: " + (provider.package_version || "未確認"), "note"));
    card.appendChild(text("p", "認証: " + (provider.login_state || "未確認") + "。ログインは端末の各CLIで行います。", "note"));
    root.appendChild(card);
  });
  if (!providers.length) {
    root.appendChild(text("p", "実CLIの起動設定がありません。WorkspaceとCLI Profileを指定して起動してください。", "note"));
  }
  refreshConnectionSelection();
}

function refreshConnectionSelection() {
  const provider = document.getElementById("wb-provider");
  const option = provider.options[provider.selectedIndex];
  document.getElementById("connection-selection").textContent =
    "現在の選択: " + (option ? option.textContent : "AI未選択") +
    " / モデル: " + (workbenchModelId() || "未選択") +
    " / 推論: " + (document.getElementById("wb-effort").value || "AIの既定値") +
    "。保存はコード編集欄で明示的に行います。";
}

function useWorkbenchForContext() {
  const provider = document.getElementById("wb-provider").value;
  const model = workbenchModelId();
  const status = document.getElementById("context-setup-status");
  if (!provider || !model) {
    status.textContent = "まずコード編集でAIとモデルを選んでください。モデル名を自動で補完しません。";
    return;
  }
  document.getElementById("preview-provider").value = provider;
  document.getElementById("preview-model").value = model;
  document.getElementById("preview-expires").value = defaultExpiry();
  status.textContent = provider + " / " + model +
    " を確認用に選びました。予算と上限は下の入力値を使います。実モデルの上限や認証を確認した値ではありません。外部送信は行いません。";
}

function transferConversationToWorkbench() {
  const status = document.getElementById("conversation-transfer-status");
  const input = document.getElementById("wb-instruction");
  if (document.getElementById("wb-body").hidden) {
    status.textContent = "コード編集のWorkspaceとCLI Profileが未設定です。指定して起動してください。";
    return;
  }
  if (input.value.trim()) {
    status.textContent = "依頼欄に入力済みの内容があるため上書きしていません。内容を確認してから依頼欄を空にしてください。";
    return;
  }
  const messages = currentConversationMessages.filter(function (message) { return message.role === "USER_TASK"; });
  if (!messages.length || messages.some(function (message) { return typeof message.body !== "string"; })) {
    status.textContent = "読み取れるUSER_TASKの本文が必要です。本文取得不可の内容を省略してコピーしません。";
    return;
  }
  input.value = messages.map(function (message) { return message.body; }).join("\n\n");
  status.textContent = "依頼欄へコピーしました。AIへの送信はまだ行っていません。対象ファイルと設定を選び、送信内容を確認してください。";
  input.scrollIntoView({block: "center"});
  input.focus();
}

document.getElementById("context-use-selection").addEventListener("click", useWorkbenchForContext);
document.getElementById("conversation-to-workbench").addEventListener("click", transferConversationToWorkbench);
["wb-model", "wb-model-custom", "wb-effort"].forEach(function (id) {
  document.getElementById(id).addEventListener("change", refreshConnectionSelection);
});


// Conversation continuity is represented by persisted source IDs, never by a CLI-native session.
function renderWorkbenchConversationChoices(sessions) {
  const select = document.getElementById("wb-thread-source");
  clear(select);
  const first = document.createElement("option");
  first.value = "";
  first.textContent = "新しい会話から始める";
  select.appendChild(first);
  sessions.forEach(function (session) {
    const option = document.createElement("option");
    option.value = session.session_id;
    option.textContent = session.provider_id + " / " + (session.target || workspacePurposeLabel(session.task_kind)) + " / " +
      session.state + " / " + session.session_id.slice(0, 8);
    select.appendChild(option);
  });
  wbSavedConversations.forEach(function (conversation) {
    const option = document.createElement("option");
    option.value = "saved:" + conversation.conversation_id;
    option.textContent = "保存した資料 / " + conversation.display_name;
    select.appendChild(option);
  });
  if (wbSourceConversationId && !Array.from(select.options).some(function (o) {
    return o.value === "saved:" + wbSourceConversationId;
  })) {
    const option = document.createElement("option");
    option.value = "saved:" + wbSourceConversationId;
    option.textContent = "現在の保存資料 / " + wbSourceConversationId.slice(0, 8);
    select.appendChild(option);
  }
  if (wbParentSessionId && !Array.from(select.options).some(function (o) {
    return o.value === wbParentSessionId;
  })) {
    const selected = document.createElement("option");
    selected.value = wbParentSessionId;
    selected.textContent = "現在の会話 / " + wbParentSessionId.slice(0, 8);
    select.appendChild(selected);
  }
  select.value = wbParentSessionId || (wbSourceConversationId ? "saved:" + wbSourceConversationId : "");
}

function renderWorkbenchConversationHistory(history) {
  const timeline = document.getElementById("wb-conversation-timeline");
  clear(timeline);
  (history.local_messages || []).forEach(function (message) {
    const card = document.createElement("details");
    card.className = "handoff-turn";
    card.appendChild(text("summary", "保存した会話 " + message.sequence + " / " + message.role));
    card.appendChild(text("pre", message.body, "mono"));
    timeline.appendChild(card);
  });
  (history.turns || []).forEach(function (turn) {
    const card = document.createElement("details");
    card.className = "handoff-turn";
    card.appendChild(text("summary", turn.sequence + ". " + turn.provider_id + " / " +
      (turn.target_relative_path || workspacePurposeLabel(turn.task_kind)) + " / " + (turn.task_kind ? "文章成果物" : turn.proposal_applied ? "適用済み" : "未適用") +
      " / " + turn.state));
    card.appendChild(text("h5", "依頼"));
    card.appendChild(text("pre", turn.instruction, "mono"));
    if (turn.task_kind) {
      if (turn.reference_text) { card.appendChild(text("h5", "参考資料")); card.appendChild(text("pre", turn.reference_text, "mono")); }
      if (turn.result_text !== null) { card.appendChild(text("h5", "生成した成果物")); card.appendChild(text("pre", turn.result_text, "mono")); }
    }
    if (turn.replacement_text !== null) {
      card.appendChild(text("h5", "生成したコード（" + (turn.proposal_applied ? "適用済み" : "未適用") + "）"));
      card.appendChild(text("pre", turn.replacement_text, "mono"));
      card.appendChild(text("h5", "当時の変更前からの差分"));
      card.appendChild(text("pre", turn.unified_diff || "差分なし", "mono"));
    }
    card.appendChild(text("h5", "適用・確認結果"));
    card.appendChild(text("pre", JSON.stringify(turn.verification, null, 2), "mono"));
    if (turn.failure) {
      card.appendChild(text("p", turn.failure.class + "：" + turn.failure.detail, "warn"));
    }
    timeline.appendChild(card);
  });
}

function refreshWorkbenchConversation() {
  const epoch = ++wbHandoffRead;
  const status = document.getElementById("wb-handoff-status");
  if (!wbParentSessionId) {
    clear(document.getElementById("wb-conversation-timeline"));
    document.getElementById("wb-thread-source").value = wbSourceConversationId ? "saved:" + wbSourceConversationId : "";
    if (wbSourceConversationId) {
      status.textContent = "保存した会話の送信範囲を確認しています…";
      return wbPost("/api/workbench/context-preview", {
        parent_session_id: null, conversation_id: wbSourceConversationId,
        history_selection: workbenchHistorySelection()
      }).then(function (result) {
        if (epoch !== wbHandoffRead) { return; }
        if (result.status !== 200) { showError(status, result); return; }
        renderWorkbenchConversationHistory(result.payload.history);
        const binding = result.payload.binding;
        const receipt = binding.selection_receipt;
        status.textContent = binding.local_message_count + "メッセージ / " +
          binding.history_bytes.toLocaleString() + " Bytesを次へ渡します。" +
          (receipt && receipt.excluded.length ? " 除外" + receipt.excluded.length +
           "件、元の会話は全保存。" : " 省略なし。");
      });
    }
    status.textContent = "新しい会話です。同じ会話を選ぶと、指定した履歴範囲を次のAIに渡します。";
    return Promise.resolve();
  }
  const source = wbParentSessionId;
  const select = document.getElementById("wb-thread-source");
  if (!Array.from(select.options).some(function (o) { return o.value === source; })) {
    const option = document.createElement("option");
    option.value = source; option.textContent = "現在の会話 / " + source.slice(0, 8);
    select.appendChild(option);
  }
  select.value = source;
  status.textContent = "会話全体と作業結果を確認しています…";
  return wbPost("/api/workbench/context-preview", {
    parent_session_id: source, conversation_id: null,
    history_selection: workbenchHistorySelection()
  }).then(function (result) {
    if (epoch !== wbHandoffRead || source !== wbParentSessionId) { return; }
    if (result.status !== 200) { showError(status, result); return; }
    renderWorkbenchConversationHistory(result.payload.history);
    const binding = result.payload.binding;
    const receipt = binding.selection_receipt;
    status.textContent = "次へ渡す履歴: " + binding.local_message_count + "メッセージ / " +
      binding.turn_count + "依頼、" + binding.history_bytes.toLocaleString() + " Bytes。" +
      (receipt && receipt.excluded.length ? " 除外 " + receipt.excluded.length + "件（元の履歴は全保存）。" : " 省略なし。") +
      (result.payload.can_continue ? " AI・モデルを切り替えて続きの依頼を入力できます。" :
        " 処理中・承認済み・結果不明の依頼があります。完了または復旧してから続けてください。");
  }).catch(function () {
    if (epoch === wbHandoffRead) {
      status.textContent = "会話を確認できません。自動送信はしていません。再読込みして確認してください。";
    }
  });
}

document.getElementById("wb-thread-source").addEventListener("change", function () {
  wbSourceConversationId = this.value.startsWith("saved:") ? this.value.slice(6) : null;
  wbParentSessionId = wbSourceConversationId ? null : (this.value || null);
  refreshWorkbenchConversation();
});
document.getElementById("wb-new-conversation").addEventListener("click", function () {
  if (wbQueueCreating || wbMutationPending) { return; }
  wbQueue = null;
  renderWorkbenchQueue();
  wbParentSessionId = null;
  wbSourceConversationId = null;
  refreshWorkbenchConversation();
});
document.getElementById("conversation-handoff-workbench").addEventListener("click", function () {
  const status = document.getElementById("conversation-transfer-status");
  if (document.getElementById("wb-body").hidden || !currentConversationId) {
    status.textContent = "保存した会話と、コード編集のWorkspace・CLI Profileが必要です。";
    return;
  }
  wbParentSessionId = null;
  wbSourceConversationId = currentConversationId;
  refreshWorkbenchConversation();
  status.textContent = "会話全体を引き継ぐよう設定しました。依頼欄は保持しています。続きの依頼を入力し、対象ファイル・AI・送信内容を確認してください。外部送信はまだ行っていません。";
  document.getElementById("wb-form").scrollIntoView({block:"start"});
  document.getElementById("wb-instruction").focus();
});


function workbenchHistorySelection() {
  const mode = document.getElementById("wb-history-mode").value;
  return mode === "recent" ?
    {mode: "recent", recent_count: Number(document.getElementById("wb-history-count").value)} :
    {mode: "full"};
}

document.getElementById("wb-history-mode").addEventListener("change", function () {
  document.getElementById("wb-history-count").disabled = this.value !== "recent";
  refreshWorkbenchConversation();
});
document.getElementById("wb-history-count").addEventListener("change", refreshWorkbenchConversation);

let chatGptPoll = null;
function renderChatGptConnection(connection) {
  const status = document.getElementById("wb-chatgpt-status");
  const login = document.getElementById("wb-chatgpt-login");
  const logout = document.getElementById("wb-chatgpt-logout");
  if (!connection) {
    status.textContent = "Workspaceの設定後にログインできます。";
    login.disabled = true; logout.disabled = true;
    return;
  }
  const labels = {DISCONNECTED: "未接続", LOGIN_PENDING: "公式ページでログイン・許可を待っています",
    CONNECTED: "ログイン済み / 利用可能なモデルを取得済み", LOGIN_FAILED: "ログインを完了できませんでした",
    LOGIN_EXPIRED: "ログインの確認期限が切れました", LOGIN_REQUIRED: "再ログインが必要です"};
  status.textContent = (labels[connection.state] || connection.state) +
    (connection.reason ? " / " + connection.reason : "") +
    (connection.remote_revocation === "UNCONFIRMED" ? " / 遠隔の切断は未確認です。ChatGPT設定で確認してください。" : "");
  login.disabled = connection.connected || connection.state === "LOGIN_PENDING";
  logout.disabled = !connection.connected && connection.state !== "LOGIN_PENDING";
  if (connection.state !== "LOGIN_PENDING") {
    document.getElementById("wb-chatgpt-auth-link").hidden = true;
  }
  if (chatGptPoll !== null) { window.clearTimeout(chatGptPoll); chatGptPoll = null; }
  if (connection.state === "LOGIN_PENDING") {
    chatGptPoll = window.setTimeout(function () { renderWorkbench(); }, 2000);
  }
}

document.getElementById("wb-chatgpt-login").addEventListener("click", async function () {
  this.disabled = true;
  const popup = window.open("about:blank", "_blank");
  if (popup) { popup.opener = null; }
  try {
    const result = await wbPost("/api/workbench/chatgpt/login", {
      new_account: document.getElementById("wb-chatgpt-new-account").checked
    });
    if (result.status !== 200) {
      if (popup) { popup.close(); }
      showError(document.getElementById("wb-chatgpt-status"), result);
      this.disabled = false;
      return;
    }
    const url = new URL(result.payload.authorization_url);
    if (url.origin !== "https://auth.openai.com" || url.pathname !== "/api/accounts/authorize" || url.username || url.password || url.hash) {
      throw new Error("unexpected login endpoint");
    }
    const link = document.getElementById("wb-chatgpt-auth-link");
    link.href = url.href; link.hidden = false;
    if (popup) { popup.location.href = url.href; }
    await renderWorkbench();
  } catch (_) {
    if (popup) { popup.close(); }
    document.getElementById("wb-chatgpt-status").textContent = "ログイン開始を確認できません。生成の送信は行っていません。";
    this.disabled = false;
  }
});

document.getElementById("wb-chatgpt-logout").addEventListener("click", async function () {
  this.disabled = true;
  const result = await wbPost("/api/workbench/chatgpt/logout", {});
  if (result.status !== 200) { showError(document.getElementById("wb-chatgpt-status"), result); this.disabled = false; return; }
  await renderWorkbench();
});


// File-free work uses the same reviewed send and durable invocation; never an apply operation.
let wbPurposes = [];
let wbArtifact = null;
function workspacePurpose() { return document.getElementById("wb-purpose").value || "writing"; }
function workspacePurposeLabel(kind) {
  const row = wbPurposes.find(function (p) { return p.task_kind === kind; });
  return row ? row.label : kind || "成果物";
}
function renderWorkspacePurposes(catalog) {
  const select = document.getElementById("wb-purpose");
  const previous = select.value || "writing";
  wbPurposes = catalog;
  clear(select);
  clear(document.getElementById("wb-purpose-cards"));
  catalog.forEach(function (purpose) {
    const option = text("option", purpose.label); option.value = purpose.task_kind; select.appendChild(option);
    const card = document.createElement("button"); card.type = "button"; card.className = "purpose-card";
    card.dataset.purpose = purpose.task_kind;
    card.appendChild(text("strong", purpose.label)); card.appendChild(text("small", purpose.description));
    card.addEventListener("click", function () { chooseWorkspacePurpose(purpose.task_kind); });
    document.getElementById("wb-purpose-cards").appendChild(card);
  });
  select.value = previous;
  syncWorkspacePurpose();
}
function chooseWorkspacePurpose(kind) {
  if (wbQueueCreating || (wbQueue && wbQueue.index < wbQueue.paths.length)) {
    document.getElementById("wb-form-error").textContent = "未着手のファイル選択を解除してから用途を変更してください。";
    return;
  }
  document.getElementById("wb-purpose").value = kind;
  syncWorkspacePurpose();
}
function syncWorkspacePurpose() {
  const purpose = wbPurposes.find(function (p) { return p.task_kind === workspacePurpose(); });
  const file = workspacePurpose() === "file_edit";
  document.getElementById("wb-file-fields").hidden = !file;
  document.getElementById("wb-target").disabled = !file;
  document.getElementById("wb-target").required = file;
  document.getElementById("wb-text-fields").hidden = file;
  document.getElementById("wb-reference").disabled = file;
  document.getElementById("wb-output-format").disabled = file;
  document.getElementById("wb-denied-details").hidden = !file;
  document.getElementById("wb-purpose-hint").textContent = purpose ? purpose.description + (file ? "。送信とファイル適用は別々に承認します。" : "。ファイル選択は不要です。生成した文章をコピー・保存できます。") : "";
  document.getElementById("wb-instruction").placeholder = purpose ? "例：" + purpose.example : "";
  const last = document.getElementById("wb-last-step");
  clear(last); last.appendChild(document.createTextNode(file ? "差分を承認・適用" : "成果物を使う"));
  last.appendChild(text("small", file ? "ファイルごとに確認" : "確認してコピー・保存"));
  document.querySelectorAll("[data-purpose]").forEach(function (card) {
    card.setAttribute("aria-pressed", String(card.dataset.purpose === workspacePurpose()));
  });
}
document.getElementById("wb-purpose").addEventListener("change", function () {
  if (wbQueueCreating || (wbQueue && wbQueue.index < wbQueue.paths.length)) {
    document.getElementById("wb-purpose").value = "file_edit";
    document.getElementById("wb-form-error").textContent = "未着手のファイル選択を解除してから用途を変更してください。";
  }
  syncWorkspacePurpose();
});
async function createTextSession() {
  const errorNode = document.getElementById("wb-form-error"); errorNode.textContent = "";
  if (wbQueue && wbQueue.index < wbQueue.paths.length) { errorNode.textContent = "未着手のファイル選択を解除してください。"; return; }
  wbQueueCreating = true; document.getElementById("wb-create").disabled = true;
  try {
    const result = await wbPost("/api/workbench/sessions", {
      provider_id: document.getElementById("wb-provider").value, model_id: workbenchModelId(),
      reasoning_effort: document.getElementById("wb-effort").value || null,
      task_kind: workspacePurpose(), instruction: document.getElementById("wb-instruction").value,
      reference_text: document.getElementById("wb-reference").value,
      output_format: document.getElementById("wb-output-format").value,
      parent_session_id: wbParentSessionId, conversation_id: wbSourceConversationId,
      history_selection: workbenchHistorySelection(), important_notes: document.getElementById("wb-important-notes").value
    });
    if (result.status !== 201) { showError(errorNode, result); return; }
    wbQueue = null; wbParentSessionId = result.payload.session_id; wbSourceConversationId = null;
    await openWorkbenchSession(result.payload.session_id);
    document.getElementById("wb-confirm-card").scrollIntoView({ block: "start" });
  } catch (_) { errorNode.textContent = "依頼作成の応答を確認できません。自動送信はしていません。履歴を確認してください。"; }
  finally { wbQueueCreating = false; document.getElementById("wb-create").disabled = false; }
}
async function loadWorkspaceResult(sessionId) {
  const result = await wbApi("/api/workbench/sessions/" + sessionId + "/result");
  if (!wbSession || wbSession.session_id !== sessionId || wbSelectedSessionId !== sessionId) { return; }
  if (result.status !== 200) { showError(document.getElementById("wb-send-error"), result); return; }
  wbArtifact = result.payload;
  document.getElementById("wb-result-text").textContent = wbArtifact.result_text;
  document.getElementById("wb-result-meta").textContent = workspacePurposeLabel(wbArtifact.task_kind) + " / " +
    wbArtifact.provider_id + " / " + wbArtifact.model_id + " / " + wbArtifact.generated_at + " / " + wbArtifact.artifact_size + " Bytes";
  document.getElementById("wb-result-action-status").textContent = "履歴に保存済みです。内容を確認してからお使いください。";
  document.getElementById("wb-result-card").hidden = false;
}
function currentWorkspaceArtifact() {
  return wbArtifact && wbSession && wbArtifact.session_id === wbSession.session_id ? wbArtifact : null;
}
document.getElementById("wb-result-copy").addEventListener("click", async function () {
  const artifact = currentWorkspaceArtifact(); if (!artifact) { return; }
  const status = document.getElementById("wb-result-action-status");
  try { await navigator.clipboard.writeText(artifact.result_text); status.textContent = "文章をコピーしました。"; }
  catch (_) { status.textContent = "クリップボードを利用できません。表示した文章を選択してコピーしてください。"; }
});
document.getElementById("wb-result-download").addEventListener("click", function () {
  const artifact = currentWorkspaceArtifact(); if (!artifact) { return; }
  const url = URL.createObjectURL(new Blob([artifact.result_text], { type: artifact.media_type + ";charset=utf-8" }));
  const link = document.createElement("a"); link.href = url; link.download = artifact.filename;
  link.click(); window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  document.getElementById("wb-result-action-status").textContent = "保存用ファイルを用意しました。ブラウザーのダウンロードを確認してください。";
});
document.getElementById("wb-result-continue").addEventListener("click", function () {
  const artifact = currentWorkspaceArtifact(); if (!artifact) { return; }
  wbParentSessionId = artifact.session_id; wbSourceConversationId = null;
  chooseWorkspacePurpose(artifact.task_kind);
  document.getElementById("wb-instruction").value = "";
  document.getElementById("wb-reference").value = "";
  refreshWorkbenchConversation();
  document.getElementById("wb-form").scrollIntoView({ block: "start" });
  document.getElementById("wb-instruction").focus();
});
