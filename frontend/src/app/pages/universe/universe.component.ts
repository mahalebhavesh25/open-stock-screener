import { Component, OnInit, OnDestroy, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { ApiService } from '../../services/api.service';

@Component({
  selector: 'app-universe',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule],
  templateUrl: './universe.component.html',
  styleUrls: ['./universe.component.css']
})
export class UniverseComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);

  instruments = signal<any[]>([]);
  totalCount = signal<number>(0);
  dataStats = signal<any>(null);
  loading = signal<boolean>(false);
  search = signal<string>('');
  page = signal<number>(1);
  pageSize = signal<number>(50);
  preflightResult = signal<any>(null);
  message = signal<string>('');
  activeTask = signal<any | null>(null);

  private pollTimer: any = null;

  ngOnInit(): void {
    this.loadData();
    this.loadStats();
    this.checkActiveUniverseTask();
  }

  ngOnDestroy(): void {
    if (this.pollTimer) clearInterval(this.pollTimer);
  }

  checkActiveUniverseTask(): void {
    this.api.getTasks().subscribe({
      next: (res) => {
        const active = (res.tasks || []).find(
          (t: any) => (t.task_type === 'UNIVERSE_BUILD' || t.task_type === 'DATA_BACKFILL') &&
                      (t.status === 'RUNNING' || t.status === 'IDLE' || t.status === 'PAUSED')
        );
        if (active) {
          this.activeTask.set(active);
          this.startTaskPolling(active.task_id || active.id);
        }
      },
      error: () => {}
    });
  }

  loadData(): void {
    this.loading.set(true);
    this.api.getUniverse(this.search(), this.page(), this.pageSize()).subscribe({
      next: (res) => {
        this.instruments.set(res.instruments || []);
        this.totalCount.set(res.total || 0);
        this.loading.set(false);
      },
      error: (err) => {
        console.error('Failed to load universe:', err);
        this.loading.set(false);
      }
    });
  }

  loadStats(): void {
    this.api.getDataStats().subscribe({
      next: (stats) => this.dataStats.set(stats),
      error: (err) => console.error('Failed to load data stats:', err)
    });
  }

  runPreflight(): void {
    this.message.set('Running Preflight diagnostic checks...');
    this.api.preflightUniverse().subscribe({
      next: (res) => {
        this.preflightResult.set(res);
        this.message.set(`Preflight checks complete. ${res.valid_symbols_count} symbols eligible.`);
      },
      error: (err) => this.message.set(`Preflight check failed: ${err.error?.detail || err.message}`)
    });
  }

  rebuildUniverse(): void {
    this.message.set('Triggering universe reconstruction task...');
    this.api.buildUniverse('full', 250).subscribe({
      next: (res) => {
        this.message.set(`Universe task started: ${res.task_id}. Tracking stage progress...`);
        this.startTaskPolling(res.task_id);
      },
      error: (err) => this.message.set(`Universe rebuild failed: ${err.error?.detail || err.message}`)
    });
  }

  triggerBackfill(): void {
    this.message.set('Triggering historical data backfill for universe...');
    this.api.backfillData([], 400).subscribe({
      next: (res) => {
        this.message.set(`Data backfill task started: ${res.task_id}. Tracking stage progress...`);
        this.startTaskPolling(res.task_id);
      },
      error: (err) => this.message.set(`Backfill trigger failed: ${err.error?.detail || err.message}`)
    });
  }

  private startTaskPolling(taskId: string): void {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = setInterval(() => {
      this.api.getTask(taskId).subscribe({
        next: (task) => {
          this.activeTask.set(task);
          if (task.status === 'COMPLETED') {
            clearInterval(this.pollTimer);
            this.message.set(`Task #${taskId} finished successfully!`);
            this.loadData();
            this.loadStats();
          } else if (task.status === 'FAILED' || task.status === 'CANCELLED') {
            clearInterval(this.pollTimer);
            this.message.set(`Task #${taskId} ended with status: ${task.status}. ${task.error_message || ''}`);
          }
        },
        error: () => {}
      });
    }, 1500);
  }

  getTaskStages(): Array<{ id: number; label: string; status: 'completed' | 'active' | 'pending' }> {
    const t = this.activeTask();
    if (!t) return [];
    const cur = t.stage_num || 1;
    const isCompleted = t.status === 'COMPLETED';

    if (t.task_type === 'UNIVERSE_BUILD') {
      const defs = ['Quotes Prefilter', 'Deep History Pull', 'SQLite Persistence'];
      return defs.map((label, idx) => {
        const stageId = idx + 1;
        let status: 'completed' | 'active' | 'pending' = 'pending';
        if (isCompleted || stageId < cur) status = 'completed';
        else if (stageId === cur) status = 'active';
        return { id: stageId, label, status };
      });
    }

    if (t.task_type === 'DATA_BACKFILL') {
      return [{ id: 1, label: 'Historical Candle Backfill', status: isCompleted ? 'completed' : 'active' }];
    }

    return [];
  }

  onSearch(): void {
    this.page.set(1);
    this.loadData();
  }

  nextPage(): void {
    if (this.page() * this.pageSize() < this.totalCount()) {
      this.page.update(p => p + 1);
      this.loadData();
    }
  }

  prevPage(): void {
    if (this.page() > 1) {
      this.page.update(p => p - 1);
      this.loadData();
    }
  }
}
