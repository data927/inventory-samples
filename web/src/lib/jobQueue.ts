type QueueTask<T> = () => Promise<T>;

export type EnqueueResult<T> = {
  position: number;
  promise: Promise<T>;
};

class InProcessJobQueue {
  private concurrency: number;
  private running = 0;
  private q: Array<{
    id: string;
    run: QueueTask<unknown>;
    resolve: (v: unknown) => void;
    reject: (e: unknown) => void;
  }> = [];

  constructor(concurrency: number) {
    this.concurrency = Math.max(1, Math.floor(concurrency || 1));
  }

  size() {
    return this.q.length;
  }

  enqueue<T>(id: string, task: QueueTask<T>): EnqueueResult<T> {
    let resolve!: (v: T) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });

    const position = this.q.length + 1;
    this.q.push({
      id,
      run: task as QueueTask<unknown>,
      resolve: resolve as unknown as (v: unknown) => void,
      reject,
    });
    this.kick();
    return { position, promise };
  }

  private kick() {
    while (this.running < this.concurrency && this.q.length > 0) {
      const next = this.q.shift()!;
      this.running += 1;
      void (async () => {
        try {
          const out = await next.run();
          next.resolve(out);
        } catch (e) {
          next.reject(e);
        } finally {
          this.running -= 1;
          this.kick();
        }
      })();
    }
  }
}

declare global {
  // eslint-disable-next-line no-var
  var __inventoryJobQueue: InProcessJobQueue | undefined;
}

export function getJobQueue() {
  const concurrency = Number(process.env.JOB_QUEUE_CONCURRENCY || "1");
  if (!globalThis.__inventoryJobQueue) {
    globalThis.__inventoryJobQueue = new InProcessJobQueue(concurrency);
  }
  return globalThis.__inventoryJobQueue;
}

