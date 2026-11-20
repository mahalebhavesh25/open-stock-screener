import { Component, OnInit, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { ApiService } from '../../services/api.service';

@Component({
  selector: 'app-screener',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule],
  templateUrl: './screener.component.html',
  styleUrls: ['./screener.component.css']
})
export class ScreenerComponent implements OnInit {
  private api = inject(ApiService);

  candidates = signal<any[]>([]);
  loading = signal<boolean>(false);
  scanning = signal<boolean>(false);
  filterStrategy = signal<string>('ALL');
  filterMinScore = signal<number>(55);
  searchQuery = signal<string>('');
  selectedCandidate = signal<any | null>(null);
  modalOpen = signal<boolean>(false);
  feedbackMsg = signal<string>('');

  filteredCandidates = computed(() => {
    const strat = this.filterStrategy();
    const isBlend = strat === 'BLEND_MOMENTUM_BREAKOUT' || strat === 'BLEND';
    const blendSet = new Set(['TREND_PULLBACK', 'VCP_BREAKOUT', 'MOMENTUM_52W']);

    return this.candidates().filter(c => {
      const matchStrat = strat === 'ALL' || (isBlend ? blendSet.has(c.strategy) : c.strategy === strat);
      const matchScore = (c.score || 0) >= this.filterMinScore();
      const matchSearch = !this.searchQuery() || c.symbol.toLowerCase().includes(this.searchQuery().toLowerCase());
      return matchStrat && matchScore && matchSearch;
    });
  });

  activeTask = signal<any | null>(null);
  private pollTimer: any = null;

  ngOnInit(): void {
    this.loadLatest();
    this.checkActiveScan();
  }

  ngOnDestroy(): void {
    if (this.pollTimer) clearInterval(this.pollTimer);
  }

  checkActiveScan(): void {
    this.api.getTasks().subscribe({
      next: (res) => {
        const active = (res.tasks || []).find(
          (t: any) => t.task_type === 'SCREENER_RUN' && (t.status === 'RUNNING' || t.status === 'IDLE' || t.status === 'PAUSED')
        );
        if (active) {
          this.activeTask.set(active);
          this.scanning.set(true);
          this.startTaskPolling(active.task_id || active.id);
        }
      },
      error: () => {}
    });
  }

  loadLatest(): void {
    this.loading.set(true);
    this.api.getLatestScreener().subscribe({
      next: (res) => {
        this.candidates.set(res.candidates || res.signals || []);
        this.loading.set(false);
      },
      error: (err) => {
        console.error('Failed to load screener candidates:', err);
        this.loading.set(false);
      }
    });
  }

  runScan(): void {
    this.scanning.set(true);
    this.feedbackMsg.set('Dispatched screener scan task to engine...');
    this.api.runScreener(true).subscribe({
      next: (res) => {
        this.feedbackMsg.set(`Screener scan started (Task #${res.task_id}). Tracking multi-stage execution...`);
        this.startTaskPolling(res.task_id);
      },
      error: (err) => {
        this.scanning.set(false);
        this.feedbackMsg.set(`Scan failed: ${err.error?.detail || err.message}`);
      }
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
            this.scanning.set(false);
            this.feedbackMsg.set(`Screener scan completed successfully! Generated ${task.message || ''}`);
            this.loadLatest();
          } else if (task.status === 'FAILED' || task.status === 'CANCELLED') {
            clearInterval(this.pollTimer);
            this.scanning.set(false);
            this.feedbackMsg.set(`Screener scan ended with status: ${task.status}. ${task.error_message || ''}`);
          }
        },
        error: () => {}
      });
    }, 1500);
  }

  getStagesList(): Array<{ id: number; label: string; status: 'completed' | 'active' | 'pending' }> {
    const t = this.activeTask();
    if (!t) return [];
    const cur = t.stage_num || 1;
    const isCompleted = t.status === 'COMPLETED';
    const defs = ['Universe Validation', 'Indicators & Features', 'Market Regime', 'Strategy Filtering', 'Position Sizing & Plans'];
    return defs.map((label, idx) => {
      const stageId = idx + 1;
      let status: 'completed' | 'active' | 'pending' = 'pending';
      if (isCompleted || stageId < cur) status = 'completed';
      else if (stageId === cur) status = 'active';
      return { id: stageId, label, status };
    });
  }

  inspectCandidate(candidate: any): void {
    this.selectedCandidate.set(candidate);
    this.modalOpen.set(true);
    // Optionally fetch full bar features
    this.api.getCandidateDetails(candidate.symbol).subscribe({
      next: (details) => {
        this.selectedCandidate.set({ ...candidate, ...details });
      },
      error: (err) => console.warn('Could not fetch deep candidate details:', err)
    });
  }

  closeModal(): void {
    this.modalOpen.set(false);
    this.selectedCandidate.set(null);
  }

  approve(candidate: any): void {
    if (!candidate.signal_id) {
      this.feedbackMsg.set('No signal ID attached to approve.');
      return;
    }
    this.api.approveSignal(candidate.signal_id).subscribe({
      next: () => {
        this.feedbackMsg.set(`Signal ${candidate.symbol} approved.`);
        this.loadLatest();
      },
      error: (err) => this.feedbackMsg.set(`Approve failed: ${err.error?.detail || err.message}`)
    });
  }

  reject(candidate: any): void {
    if (!candidate.signal_id) {
      this.feedbackMsg.set('No signal ID attached to reject.');
      return;
    }
    this.api.rejectSignal(candidate.signal_id, 'Rejected in screener').subscribe({
      next: () => {
        this.feedbackMsg.set(`Signal ${candidate.symbol} rejected.`);
        this.loadLatest();
      },
      error: (err) => this.feedbackMsg.set(`Reject failed: ${err.error?.detail || err.message}`)
    });
  }
}
