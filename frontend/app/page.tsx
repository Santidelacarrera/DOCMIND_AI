import Link from "next/link";
const capabilities = [
  "Secure PDF ingestion",
  "Asynchronous processing",
  "Human review and exports",
];
export default function Home() {
  return (
    <main>
      <nav>
        <strong>DocMind AI</strong>
        <Link href="/documents">Documents</Link>
      </nav>
      <section className="hero">
        <p className="eyebrow">INTELLIGENT DOCUMENT PROCESSING</p>
        <h1>Turn business documents into reviewable data.</h1>
        <p>
          Upload PDFs, track processing, validate extracted facts, and export
          structured results—without losing source context.
        </p>
        <Link className="button" href="/documents">
          Open workspace
        </Link>
      </section>
      <section className="grid">
        {capabilities.map((x) => (
          <article key={x}>
            <h2>{x}</h2>
            <p>
              Built with tenant boundaries and a durable audit-ready processing
              pipeline.
            </p>
          </article>
        ))}
      </section>
    </main>
  );
}
