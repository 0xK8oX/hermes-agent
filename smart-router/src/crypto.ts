/**
 * Smart Router - Encryption utilities
 *
 * AES-256-GCM encryption for API keys stored in D1.
 * Master key (KEY_ENCRYPTION_KEY) is kept in Wrangler secrets.
 */

const ALGORITHM = "AES-GCM";
const KEY_LENGTH = 256;
const IV_LENGTH = 12; // 96 bits for GCM

async function getMasterKey(env: Env): Promise<CryptoKey> {
  const b64 = (env as unknown as Record<string, string>).KEY_ENCRYPTION_KEY;
  if (!b64) {
    throw new Error("Missing KEY_ENCRYPTION_KEY in secrets");
  }
  const raw = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  if (raw.length !== 32) {
    throw new Error("KEY_ENCRYPTION_KEY must be 32 bytes (base64-encoded)");
  }
  return crypto.subtle.importKey(
    "raw",
    raw,
    { name: ALGORITHM, length: KEY_LENGTH },
    false,
    ["encrypt", "decrypt"]
  );
}

/**
 * Encrypt a plaintext API key.
 * Returns base64(iv + ciphertext + auth_tag).
 */
export async function encryptKey(
  plaintext: string,
  env: Env
): Promise<string> {
  const key = await getMasterKey(env);
  const iv = crypto.getRandomValues(new Uint8Array(IV_LENGTH));
  const encoded = new TextEncoder().encode(plaintext);
  const ciphertext = await crypto.subtle.encrypt(
    { name: ALGORITHM, iv },
    key,
    encoded
  );

  const combined = new Uint8Array(iv.length + ciphertext.byteLength);
  combined.set(iv);
  combined.set(new Uint8Array(ciphertext), iv.length);
  return btoa(String.fromCharCode(...combined));
}

/**
 * Decrypt an encrypted API key.
 * Input is base64(iv + ciphertext + auth_tag).
 */
export async function decryptKey(
  encryptedB64: string,
  env: Env
): Promise<string> {
  const key = await getMasterKey(env);
  const combined = Uint8Array.from(atob(encryptedB64), (c) =>
    c.charCodeAt(0)
  );
  const iv = combined.slice(0, IV_LENGTH);
  const ciphertext = combined.slice(IV_LENGTH);
  const decrypted = await crypto.subtle.decrypt(
    { name: ALGORITHM, iv },
    key,
    ciphertext
  );
  return new TextDecoder().decode(decrypted);
}
