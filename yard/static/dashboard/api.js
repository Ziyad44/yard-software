export async function apiGet(path) {
  const response = await fetch(path, { method: "GET" });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function apiPost(path, payload = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || "Request failed");
  }
  return response.json();
}
