"use strict";

// The export is parsed locally. Only the selected, visible reference is sent to this server.
function chatGptExportList(value) {
  const items = Array.isArray(value) ? value : [value];
  if (!items.length || items.length > 10000 ||
      items.some(function (item) {
        return !item || typeof item !== "object" || Array.isArray(item) ||
          !item.mapping || typeof item.mapping !== "object" || Array.isArray(item.mapping);
      })) {
    throw new Error("会話一覧の形式を確認できません。展開済みのconversations.jsonを選んでください。");
  }
  return items;
}

function chatGptCurrentBranch(item) {
  const mapping = item.mapping;
  const keys = Object.keys(mapping);
  if (!keys.length || keys.length > 10000 || typeof item.current_node !== "string") {
    throw new Error("現在の分岐や会話のサイズを確認できません。文章として選んで貼り付けてください。");
  }
  const visited = new Set();
  const chain = [];
  let cursor = item.current_node;
  while (cursor !== null) {
    if (typeof cursor !== "string" || !Object.prototype.hasOwnProperty.call(mapping, cursor) ||
        visited.has(cursor)) {
      throw new Error("会話の分岐が欠落または循環しています。推測して取り込みません。");
    }
    visited.add(cursor);
    const node = mapping[cursor];
    if (!node || typeof node !== "object" || Array.isArray(node) ||
        !(node.parent === null || typeof node.parent === "string")) {
      throw new Error("会話の接続を確認できません。文章として選んで貼り付けてください。");
    }
    chain.push(node);
    cursor = node.parent;
  }
  const blocks = [];
  let omittedMessages = 0;
  let omittedParts = 0;
  chain.reverse().forEach(function (node) {
    if (node.message === null) { return; }
    const message = node.message;
    const role = message && message.author && message.author.role;
    const content = message && message.content;
    if (message && message.metadata && Array.isArray(message.metadata.attachments)) {
      omittedParts += message.metadata.attachments.length;
    }
    if (!["user", "assistant", "system", "developer", "tool"].includes(role) || !content ||
        !["text", "multimodal_text"].includes(content.content_type) ||
        !Array.isArray(content.parts)) {
      omittedMessages += 1;
      return;
    }
    const parts = [];
    content.parts.forEach(function (part) {
      if (typeof part === "string") { parts.push(part); } else { omittedParts += 1; }
    });
    if (!parts.some(function (part) { return part.trim(); })) {
      omittedMessages += 1;
      return;
    }
    blocks.push("[" + role + " / 元の会話内のラベル・権限なし]\n" + parts.join("\n"));
  });
  if (!blocks.length) { throw new Error("選択した分岐に取り込めるテキストがありません。"); }
  return {
    title: typeof item.title === "string" && item.title.trim() ? item.title : "ChatGPTからの会話",
    content: blocks.join("\n\n"),
    omission_note: "現在の分岐のテキストのみ。分岐内ノード " + chain.length +
      "、分岐外ノード " + (keys.length - chain.length) + "。非対応・空のメッセージ " +
      omittedMessages + "、画像・添付など非テキスト部分 " + omittedParts +
      " は取り込んでいません。その他のメタデータや添付の実体も対象外です。本文は利用者が編集できます。",
    omitted_messages: omittedMessages,
    omitted_parts: omittedParts,
    other_nodes: keys.length - chain.length
  };
}

let knowledgePreview = null;
let knowledgeEpoch = 0;
let knowledgeFileEpoch = 0;
let knowledgeSaving = false;
let knowledgeSavedId = null;
let knowledgeExports = [];
let knowledgeOmissionNote = "";

function knowledgeFields() {
  return {
    source_kind: document.getElementById("knowledge-kind").value,
    title: document.getElementById("knowledge-title").value,
    content: document.getElementById("knowledge-content").value,
    omission_note: knowledgeOmissionNote
  };
}

function invalidateKnowledgePreview() {
  knowledgeEpoch += 1;
  knowledgePreview = null;
  knowledgeSavedId = null;
  document.getElementById("knowledge-use").hidden = true;
  document.getElementById("knowledge-review").hidden = true;
  document.getElementById("knowledge-preview-body").textContent = "";
  document.getElementById("knowledge-approve-save").checked = false;
  document.getElementById("knowledge-save").disabled = true;
}

function setKnowledgeBusy(busy) {
  knowledgeSaving = busy;
  ["knowledge-kind", "knowledge-file", "knowledge-export-choice", "knowledge-title",
   "knowledge-content", "knowledge-preview", "knowledge-approve-save"].forEach(function (id) {
    document.getElementById(id).disabled = busy;
  });
  document.getElementById("knowledge-save").disabled = busy ||
    !knowledgePreview || !document.getElementById("knowledge-approve-save").checked;
}

function chooseKnowledgeExport() {
  invalidateKnowledgePreview();
  try {
    const selected = chatGptCurrentBranch(
      knowledgeExports[Number(document.getElementById("knowledge-export-choice").value)]
    );
    document.getElementById("knowledge-kind").value = "chatgpt_conversation";
    document.getElementById("knowledge-title").value = selected.title;
    document.getElementById("knowledge-content").value = selected.content;
    knowledgeOmissionNote = selected.omission_note;
    document.getElementById("knowledge-source-status").textContent = selected.omission_note;
    document.getElementById("knowledge-status").textContent =
      "この会話の表示した内容だけを確認・保存します。書き出しファイル全体はサーバーへ送りません。";
  } catch (error) {
    document.getElementById("knowledge-content").value = "";
    document.getElementById("knowledge-status").textContent = error.message;
  }
}

document.getElementById("knowledge-export-choice").addEventListener("change", chooseKnowledgeExport);
["knowledge-kind", "knowledge-title", "knowledge-content"].forEach(function (id) {
  document.getElementById(id).addEventListener("input", function () {
    knowledgeFileEpoch += 1;
    invalidateKnowledgePreview();
  });
});
document.getElementById("knowledge-kind").addEventListener("change", function () {
  knowledgeFileEpoch += 1;
  knowledgeExports = [];
  knowledgeOmissionNote = "利用者が選んで貼り付けた参考資料。表示した本文だけを保存します。";
  document.getElementById("knowledge-file").value = "";
  document.getElementById("knowledge-export-choice").hidden = true;
  document.getElementById("knowledge-choice-label").hidden = true;
  document.getElementById("knowledge-source-status").textContent = knowledgeOmissionNote;
  invalidateKnowledgePreview();
});
document.getElementById("knowledge-file").addEventListener("change", async function () {
  const epoch = ++knowledgeFileEpoch;
  invalidateKnowledgePreview();
  const inputEpoch = knowledgeEpoch;
  const file = this.files[0];
  if (!file) { return; }
  const status = document.getElementById("knowledge-status");
  const select = document.getElementById("knowledge-export-choice");
  select.hidden = true;
  document.getElementById("knowledge-choice-label").hidden = true;
  document.getElementById("knowledge-content").value = "";
  knowledgeExports = [];
  knowledgeOmissionNote = "";
  document.getElementById("knowledge-source-status").textContent = "ファイルをローカルで確認しています…";
  try {
    if (file.size > 16 * 1024 * 1024) {
      throw new Error("読込み上限は16MiBです。大きい会話は必要な本文を選んで貼り付けてください。");
    }
    if (!/\.(json|txt|md)$/i.test(file.name)) {
      throw new Error("展開済みの.jsonか、.txt/.mdを選んでください。ZIPは先に展開してください。");
    }
    const bytes = await file.arrayBuffer();
    const raw = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
    if (epoch !== knowledgeFileEpoch || inputEpoch !== knowledgeEpoch) { return; }
    if (/\.json$/i.test(file.name)) {
      knowledgeExports = chatGptExportList(JSON.parse(raw));
      clear(select);
      knowledgeExports.forEach(function (item, index) {
        const option = document.createElement("option");
        option.value = String(index);
        option.textContent = String(index + 1) + ". " +
          (typeof item.title === "string" ? item.title : "無題の会話");
        select.appendChild(option);
      });
      select.hidden = false;
      document.getElementById("knowledge-choice-label").hidden = false;
      chooseKnowledgeExport();
    } else {
      document.getElementById("knowledge-title").value = file.name;
      document.getElementById("knowledge-content").value = raw;
      knowledgeOmissionNote = "利用者が選んだText/Markdown。本文全体を表示。";
      document.getElementById("knowledge-source-status").textContent = knowledgeOmissionNote;
      status.textContent = "ローカルで読み込みました。保存前に内容を確認してください。";
    }
  } catch (error) {
    if (epoch === knowledgeFileEpoch) {
      knowledgeExports = [];
      document.getElementById("knowledge-content").value = "";
      document.getElementById("knowledge-source-status").textContent = "ファイルの取込は完了していません。";
      status.textContent = error.message;
    }
  }
});

document.getElementById("knowledge-preview").addEventListener("click", async function () {
  invalidateKnowledgePreview();
  const epoch = knowledgeEpoch;
  const fields = knowledgeFields();
  const fingerprint = JSON.stringify(fields);
  const status = document.getElementById("knowledge-status");
  if (new TextEncoder().encode(fields.content).byteLength > 64 * 1024) {
    status.textContent = "本文はUTF-8で64KiBまでです。必要な知識を選んでから再確認してください。";
    return;
  }
  status.textContent = "ローカルの内容検査中です。AIへは送りません…";
  try {
    const result = await wbPost("/api/knowledge/preview", fields);
    if (epoch !== knowledgeEpoch || fingerprint !== JSON.stringify(knowledgeFields())) { return; }
    if (result.status !== 200) { showError(status, result); return; }
    knowledgePreview = {fields: fields, fingerprint: fingerprint, hash: result.payload.preview_hash};
    document.getElementById("knowledge-preview-body").textContent = result.payload.body;
    document.getElementById("knowledge-preview-meta").textContent =
      result.payload.size_bytes.toLocaleString() + " Bytes / " + result.payload.preview_hash;
    document.getElementById("knowledge-review").hidden = false;
    status.textContent = "保存する完全な内容を表示しています。保存はローカルのみで、CLIは起動しません。";
  } catch (error) {
    if (epoch === knowledgeEpoch) {
      status.textContent = "検査結果を取得できません。保存していません。接続を確認してください。";
    }
  }
});

document.getElementById("knowledge-approve-save").addEventListener("change", function () {
  document.getElementById("knowledge-save").disabled = knowledgeSaving || !knowledgePreview || !this.checked;
});
document.getElementById("knowledge-save").addEventListener("click", async function () {
  if (knowledgeSaving || !knowledgePreview ||
      !document.getElementById("knowledge-approve-save").checked ||
      knowledgePreview.fingerprint !== JSON.stringify(knowledgeFields())) { return; }
  const preview = knowledgePreview;
  const status = document.getElementById("knowledge-status");
  setKnowledgeBusy(true);
  let outcomeUnknown = false;
  try {
    const result = await wbPost("/api/knowledge/import", Object.assign({}, preview.fields, {
      preview_hash: preview.hash, approve_save: true
    }));
    if (result.status !== 201) { showError(status, result); return; }
    invalidateKnowledgePreview();
    knowledgeSavedId = result.payload.conversation.conversation_id;
    document.getElementById("knowledge-use").hidden = false;
    status.textContent = "知識を保存しました。会話ID: " + knowledgeSavedId +
      "。AIへの送信はまだです。「この知識で依頼する」から続けられます。";
    await renderConversations();
    await renderWorkbench();
  } catch (error) {
    outcomeUnknown = true;
    invalidateKnowledgePreview();
    status.textContent = "保存結果を確認できません。再保存せず、会話一覧を再読込みして確認してください。";
  } finally {
    setKnowledgeBusy(false);
    if (outcomeUnknown) { document.getElementById("knowledge-save").disabled = true; }
  }
});
document.getElementById("knowledge-use").addEventListener("click", function () {
  if (!knowledgeSavedId || wbQueueCreating || wbMutationPending) { return; }
  wbParentSessionId = null;
  wbSourceConversationId = knowledgeSavedId;
  refreshWorkbenchConversation();
  document.getElementById("wb-form").scrollIntoView({block: "start"});
  document.getElementById("wb-instruction").focus();
});
