import { Component, OnInit, OnDestroy, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule, Router } from '@angular/router';
import { ApiService, OverviewData } from '../../services/api.service';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, RouterModule],
  templateUrl: './dashboard.component.html',
  styleUrls: ['./dashboard.component.css']
})
export class DashboardComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private router = inject(Router);
  overview = signal<OverviewData | null>(null);
  activeTasks = computed(() => this.overview()?.active_tasks || []);
  pendingSignals = signal<any[]>([]);
  openTrades = signal<any[]>([]);
  loading = signal<boolean>(true);
  actionMessage = signal<string>('');
  private intervalId: any = null;

  ngOnInit(): void {
    this.refresh();
    this.intervalId = setInterval(() => this.refresh(), 3000);
  }

  ngOnDestroy(): void {
    if (this.intervalId) clearInterval(this.intervalId);
  }

  refresh(): void {
    this.api.getOverview().subscribe({
      next: (data) => {
        this.overview.set(data);
        this.loading.set(false);
      },
      error: (err) => {
        console.error('Failed to load overview:', err);
        // Do not set overview to null; keep existing view intact
      }
    });


    this.api.getTrades('PROPOSED').subscribe({
      next: (data) => this.pendingSignals.set(data.trades || []),
      error: (err) => console.error('Failed to load proposed trades:', err)
    });

    this.api.getTrades('OPEN').subscribe({
      next: (data) => this.openTrades.set(data.trades || []),
      error: (err) => console.error('Failed to load open trades:', err)
    });
  }

  approveTrade(trade: any): void {
    if (!trade.signal_id) return;
    this.actionMessage.set(`Approving ${trade.symbol}...`);
    this.api.approveSignal(trade.signal_id).subscribe({
      next: () => {
        this.actionMessage.set(`Trade ${trade.symbol} approved.`);
        this.refresh();
      },
      error: (err) => this.actionMessage.set(`Approval failed: ${err.error?.detail || err.message}`)
    });
  }

  rejectTrade(trade: any): void {
    if (!trade.signal_id) return;
    this.actionMessage.set(`Rejecting ${trade.symbol}...`);
    this.api.rejectSignal(trade.signal_id, 'User rejected from dashboard').subscribe({
      next: () => {
        this.actionMessage.set(`Trade ${trade.symbol} rejected.`);
        this.refresh();
      },
      error: (err) => this.actionMessage.set(`Rejection failed: ${err.error?.detail || err.message}`)
    });
  }

  triggerScreener(): void {
    this.actionMessage.set('Triggering full pipeline screener scan...');
    this.api.runScreener(true).subscribe({
      next: (res) => {
        this.actionMessage.set(`Screener task #${res.task_id} initiated. Opening Screener pipeline...`);
        this.router.navigate(['/screener']);
      },
      error: (err) => this.actionMessage.set(`Screener trigger failed: ${err.error?.detail || err.message}`)
    });
  }

  triggerPreflight(): void {
    this.actionMessage.set('Running universe preflight check...');
    this.api.preflightUniverse().subscribe({
      next: (res) => this.actionMessage.set(`Preflight complete: ${res.valid_symbols_count} symbols eligible.`),
      error: (err) => this.actionMessage.set(`Preflight check failed: ${err.error?.detail || err.message}`)
    });
  }
}
