const secrets = new Set();
const secretField = String.raw`(?:(?:api|access|secret|private)[_-]?key(?:[_-]?id)?|secret[_-]?access[_-]?key|password|passwd|secret|token|authorization|cookie|credentials?|signature)`;
const secretName = new RegExp(secretField + "$", "i");
const quotedSecret = new RegExp(String.raw`(\b[A-Za-z0-9_-]{0,64}${secretField}["']?\s*[:=]\s*)("(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*')`, "gi");
const plainSecret = new RegExp(String.raw`(\b[A-Za-z0-9_-]{0,64}${secretField}["']?\s*[:=]\s*["']?)([^\s,"'}]+)`, "gi");
const urlSecret = new RegExp(String.raw`([?&](?:[A-Za-z0-9_-]{0,64}${secretField}|key|sig)=)[^&#\s"']+`, "gi");

export function isSecretName(name) {
  return secretName.test(name);
}

export function registerLogSecrets(values = []) {
  for (const value of values) if (typeof value === "string" && value) secrets.add(value);
}

export function redactLogText(value) {
  let text = String(value).replace(/\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "").replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g, "");
  for (const secret of [...secrets].sort((a, b) => b.length - a.length)) {
    if (secret.length >= 8) text = text.split(secret).join("[REDACTED]");
    else {
      const escaped = secret.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      text = text.replace(new RegExp(String.raw`(^|[^\w./\\-])${escaped}(?=$|[^\w./\\-]|\.(?![\w/\\]))`, "g"), "$1[REDACTED]");
    }
  }
  return text
    .replace(/\bsk-[A-Za-z0-9_-]+/g, "[REDACTED]")
    .replace(/\b(Bearer|Basic)\s+[^\s"'<>]+/gi, "$1 [REDACTED]")
    .replace(/(https?:\/\/)[^\s/@]+@/gi, "$1[REDACTED]@")
    .replace(urlSecret, "$1[REDACTED]")
    .replace(quotedSecret, '$1"[REDACTED]"')
    .replace(plainSecret, "$1[REDACTED]");
}
