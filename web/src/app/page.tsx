import JobsTableClient from "@/components/JobsTableClient";
import RunPanelClient from "@/components/RunPanelClient";
import { getApiBase } from "@/lib/apiBase";

const AppName = "Inventory Intelligence";
const Card =
  "rounded-2xl border border-zinc-200/70 dark:border-zinc-800/70 bg-white dark:bg-zinc-950 shadow-sm";
const CardBody = "p-6";
const Title = "text-base font-semibold text-zinc-900 dark:text-zinc-50";
const Subtle = "text-sm text-zinc-600 dark:text-zinc-400";

export default function Home() {
  const apiBase = getApiBase();
  // Client-only interactions without bringing in a component library.
  return (
    <div className="relative flex min-h-svh flex-1 flex-col overflow-hidden">
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 z-0 bg-ambient" />
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 z-0 bg-ambient-aurora" />
      <div className="relative z-10 flex flex-1 flex-col">
        <header className="border-b border-zinc-200/70 bg-white/75 backdrop-blur-md dark:border-zinc-800/70 dark:bg-zinc-950/55">
          <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
            <div className="flex items-center gap-3">
              <div className="h-9 w-9 rounded-xl bg-linear-to-br from-zinc-900 to-zinc-600 dark:from-zinc-100 dark:to-zinc-400" />
              <div>
                <div className="text-lg font-semibold text-zinc-900 dark:text-zinc-50">
                  {AppName}
                </div>
                <div className="text-sm text-zinc-500 dark:text-zinc-400">
                  Build & segment inventories with auditable evidence.
                </div>
              </div>
            </div>
            <a
              href={`${apiBase}/api/health`}
              className="text-sm text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
            >
              API health
            </a>
          </div>
        </header>

        <main className="mx-auto w-full max-w-6xl px-6 py-10">
          <div className="flex flex-col gap-10">
            <RunPanelClient />

            <section className={`${Card} ${CardBody}`}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <h2 className={Title}>Jobs</h2>
                  <p className={`mt-1 ${Subtle}`}>
                    Runs are tracked locally; the list auto-refreshes while jobs run.
                  </p>
                </div>
                <a
                  href={`${apiBase}/api/jobs`}
                  className="shrink-0 text-sm text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
                >
                  View JSON
                </a>
              </div>

              <div className="mt-5 overflow-x-auto">
                <JobsTableClient />
              </div>
            </section>
          </div>
        </main>
      </div>
    </div>
  );
}

