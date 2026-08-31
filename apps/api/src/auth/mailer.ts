import nodemailer from "nodemailer";
import type { PasswordAuthConfig } from "./config.js";

export type AuthMailResult = {
  testToken?: string;
};

export class AuthMailer {
  constructor(private readonly config: PasswordAuthConfig) {}

  async sendVerification(input: { email: string; token: string }): Promise<AuthMailResult> {
    const url = `${this.config.publicBaseUrl.replace(/\/$/u, "")}/data-tasks?verify=${encodeURIComponent(input.token)}`;
    return this.send({
      email: input.email,
      subject: "Verify your DataFoundry account",
      text: `Verify your DataFoundry account: ${url}`,
      token: input.token
    });
  }

  async sendPasswordReset(input: { email: string; token: string }): Promise<AuthMailResult> {
    const url = `${this.config.publicBaseUrl.replace(/\/$/u, "")}/data-tasks?reset=${encodeURIComponent(input.token)}`;
    return this.send({
      email: input.email,
      subject: "Reset your DataFoundry password",
      text: `Reset your DataFoundry password: ${url}`,
      token: input.token
    });
  }

  private async send(input: {
    email: string;
    subject: string;
    text: string;
    token: string;
  }): Promise<AuthMailResult> {
    if (this.config.emailDelivery === "test") {
      return { testToken: input.token };
    }
    if (!this.config.smtp) {
      throw new Error("AUTH_CONFIG_MISSING:SMTP is required.");
    }
    const transport = nodemailer.createTransport({
      host: this.config.smtp.host,
      port: this.config.smtp.port,
      secure: this.config.smtp.secure,
      auth: this.config.smtp.user
        ? { user: this.config.smtp.user, pass: this.config.smtp.password ?? "" }
        : undefined
    });
    await transport.sendMail({
      from: this.config.smtp.from,
      to: input.email,
      subject: input.subject,
      text: input.text
    });
    return {};
  }
}
