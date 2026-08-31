import argon2 from "argon2";
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

const PASSWORD_HASH_PARAMS = {
  algorithm: "argon2id",
  memoryCost: 19456,
  parallelism: 1,
  timeCost: 2
} as const;

export async function hashPassword(password: string): Promise<{ hash: string; params: string }> {
  const hash = await argon2.hash(password, {
    type: argon2.argon2id,
    memoryCost: PASSWORD_HASH_PARAMS.memoryCost,
    parallelism: PASSWORD_HASH_PARAMS.parallelism,
    timeCost: PASSWORD_HASH_PARAMS.timeCost
  });
  return { hash, params: JSON.stringify(PASSWORD_HASH_PARAMS) };
}

export async function verifyPassword(hash: string, password: string): Promise<boolean> {
  return argon2.verify(hash, password);
}

export function createSecretToken(): string {
  return randomBytes(32).toString("base64url");
}

export function hashToken(token: string, secret: string): string {
  return createHmac("sha256", secret).update(token).digest("base64url");
}

export function constantTimeEqual(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  return leftBuffer.length === rightBuffer.length && timingSafeEqual(leftBuffer, rightBuffer);
}
