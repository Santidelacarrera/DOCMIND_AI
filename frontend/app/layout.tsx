import type { Metadata } from "next";
import "./styles.css";
export const metadata: Metadata = {
  title: "DocMind AI",
  description: "Intelligent document processing",
};
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
