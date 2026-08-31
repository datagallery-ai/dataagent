import type { Metadata } from "next";
import { RegisterClient } from "./register-client";

export const metadata: Metadata = {
  title: "Sign up",
};

export default function RegisterPage() {
  return <RegisterClient />;
}
