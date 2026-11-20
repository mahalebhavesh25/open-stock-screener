import { Component, OnInit, OnDestroy, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api.service';

export interface LogEntry {
  timestamp: string;
  level: string;
  source: string;
  message: string;
}

export interface TaskStageItem {
  id: number;
  label: string;
  status: 'completed' | 'active' | 'pending';
}

export interface SystemTask {
  task_id: string;
  task_type: string;
  status: string;
  progress_pct: number;
  current_step: string;
  total_steps: number;
  completed_steps: number;
  created_at: string;
  updated_at: string;
  stage?: string;
  stage_num?: number;
  total_stages?: number;
  stage_progress_pct?: number;
  error_message?: string;
  id?: string;
  error?: string;
  message?: string;
  current_item?: string;
  completed_items?: number;
  total_items?: number;
}

@Component({
  selector: 'app-tasks',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './tasks.component.html',
  styleUrls: ['./tasks.component.css']
})
export class TasksComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);

  tasks = signal<SystemTask[]>([]);
  logs = signal<LogEntry[]>([]);
  logFilter = signal<string>('');
  logLevel = signal<string>('ALL');
  autoScroll = signal<boolean>(true);
  message = signal<string>('');

  filteredLogs = computed(() => {
    const q = this.logFilter().toLowerCase().trim();
    const lvl = this.logLevel();
    return this.logs().filter(entry => {
      const matchLvl = lvl === 'ALL' || entry.level === lvl;
      const matchQ = !q ||
        entry.message.toLowerCase().includes(q) ||
        entry.source.toLowerCase().includes(q) ||
        entry.timestamp.includes(q);
      return matchLvl && matchQ;
    });
  });

  private eventSource: EventSource | null = null;
  private pollInterval: any = null;

  ngOnInit(): void {
    this.loadTasks();
    this.loadRecentLogs();
    this.pollInterval = setInterval(() => this.loadTasks(), 2500);
    this.connectLogStream();
  }

  ngOnDestroy(): void {
    if (this.pollInterval) clearInterval(this.pollInterval);
    if (this.eventSource) this.eventSource.close();
  }

  loadTasks(): void {
    this.api.getTasks().subscribe({
      next: (res) => this.tasks.set(res.tasks || []),
      error: (err) => console.error('Failed to load tasks:', err)
    });
  }

  loadRecentLogs(): void {
    this.api.getRecentLogs(80).subscribe({
      next: (res) => {
        if (res.logs && res.logs.length > 0) {
          const parsed = res.logs.map(l => this.normalizeLog(l));
          this.logs.set(parsed);
          this.triggerAutoScroll();
        }
      },
      error: (err) => console.warn('Could not load recent logs:', err)
    });
  }

  connectLogStream(): void {
    try {
      this.eventSource = new EventSource(this.api.getLogStreamUrl());
      this.eventSource.onmessage = (event) => {
        if (event.data) {
          const entry = this.normalizeLog(event.data);
          this.logs.update((current) => [...current.slice(-400), entry]);
          if (this.autoScroll()) {
            this.triggerAutoScroll();
          }
        }
      };
      this.eventSource.onerror = () => {
        // SSE error or reconnection
      };
    } catch (e) {
      console.warn('SSE log connection error:', e);
    }
  }

  private triggerAutoScroll(): void {
    setTimeout(() => {
      const el = document.getElementById('log-console-box');
      if (el) el.scrollTop = el.scrollHeight;
    }, 40);
  }

  normalizeLog(raw: any): LogEntry {
    if (typeof raw === 'object' && raw !== null && raw.message) {
      return {
        timestamp: raw.timestamp || '',
        level: (raw.level || 'INFO').toUpperCase(),
        source: raw.source || 'engine',
        message: raw.message || '',
      };
    }
    if (typeof raw === 'string') {
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          return {
            timestamp: parsed.timestamp || '',
            level: (parsed.level || 'INFO').toUpperCase(),
            source: parsed.source || 'engine',
            message: parsed.message || '',
          };
        }
      } catch {
        // plain text log
      }
      return {
        timestamp: '',
        level: 'INFO',
        source: 'engine',
        message: raw
      };
    }
    return {
      timestamp: '',
      level: 'INFO',
      source: 'engine',
      message: String(raw)
    };
  }

  getTaskStages(t: SystemTask): TaskStageItem[] {
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

    if (t.task_type === 'SCREENER_RUN') {
      const defs = ['Universe', 'Panel Features', 'Market Regime', 'Strategy Scan', 'Trade Plans'];
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

    if (t.task_type === 'BACKTEST_RUN') {
      const total = t.total_stages || 6;
      const stages: TaskStageItem[] = [];
      for (let i = 1; i <= Math.min(total, 8); i++) {
        let status: 'completed' | 'active' | 'pending' = 'pending';
        if (isCompleted || i < cur) status = 'completed';
        else if (i === cur) status = 'active';
        stages.push({ id: i, label: `Fold ${i}`, status });
      }
      return stages;
    }

    // Default fallback
    const total = t.total_stages || 1;
    const stages: TaskStageItem[] = [];
    for (let i = 1; i <= total; i++) {
      let status: 'completed' | 'active' | 'pending' = 'pending';
      if (isCompleted || i < cur) status = 'completed';
      else if (i === cur) status = 'active';
      stages.push({ id: i, label: `Stage ${i}`, status });
    }
    return stages;
  }

  pause(task: SystemTask): void {
    const id = task.task_id || (task as any).id;
    this.api.pauseTask(id).subscribe({
      next: () => {
        this.message.set(`Task ${id} paused.`);
        this.loadTasks();
      },
      error: (err) => this.message.set(`Pause failed: ${err.error?.detail || err.message}`)
    });
  }

  resume(task: SystemTask): void {
    const id = task.task_id || (task as any).id;
    this.api.resumeTask(id).subscribe({
      next: () => {
        this.message.set(`Task ${id} resumed from last checkpoint.`);
        this.loadTasks();
      },
      error: (err) => this.message.set(`Resume failed: ${err.error?.detail || err.message}`)
    });
  }

  cancel(task: SystemTask): void {
    const id = task.task_id || (task as any).id;
    if (!confirm(`Cancel task ${id}?`)) return;
    this.api.cancelTask(id).subscribe({
      next: () => {
        this.message.set(`Task ${id} cancelled.`);
        this.loadTasks();
      },
      error: (err) => this.message.set(`Cancel failed: ${err.error?.detail || err.message}`)
    });
  }

  deleteTask(task: SystemTask): void {
    const id = task.task_id || (task as any).id;
    this.api.deleteTask(id).subscribe({
      next: () => {
        this.message.set(`Task ${id} removed.`);
        this.loadTasks();
      },
      error: (err) => this.message.set(`Delete failed: ${err.error?.detail || err.message}`)
    });
  }

  clearFinishedTasks(): void {
    this.api.clearTasks('finished').subscribe({
      next: (res) => {
        this.message.set(`Cleared ${res.count || 0} finished task(s).`);
        this.loadTasks();
      },
      error: (err) => this.message.set(`Clear failed: ${err.error?.detail || err.message}`)
    });
  }

  clearLogs(): void {
    this.logs.set([]);
  }
}
