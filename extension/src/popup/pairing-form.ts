// The popup never touches the device credential itself: pairing and sign-out
// are requests to the background worker (Bundle 7 spec §9.2, X5).
export type PairingResult = { ok: true; accountLabel: string } | { ok: false; message: string };
export type SendToBackground = (message: Record<string, unknown>) => Promise<any>;

export async function attemptPairing(code: string, send: SendToBackground): Promise<PairingResult> {
  const trimmed = code.trim();
  if (!trimmed) return { ok: false, message: "Enter the pairing code shown in JobSearch." };
  try {
    const response = await send({ type: "pair", code: trimmed, label: "Browser extension" });
    if (response?.ok) return { ok: true, accountLabel: response.accountLabel };
    return { ok: false, message: response?.message ?? "Pairing failed." };
  } catch (err) {
    return { ok: false, message: err instanceof Error ? err.message : String(err) };
  }
}

export async function attemptSignOut(send: SendToBackground): Promise<void> {
  await send({ type: "sign_out" });
}
