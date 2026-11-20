import { Component, OnInit, OnDestroy, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterOutlet, RouterModule } from '@angular/router';
import { ApiService, OverviewData } from './services/api.service';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, RouterOutlet, RouterModule],
  templateUrl: './app.html',
  styleUrls: ['./app.css']
})
export class App implements OnInit, OnDestroy {
  private api = inject(ApiService);

  overview = signal<OverviewData | null>(null);
  apiOnline = signal<boolean>(false);
  activeTasks = computed(() => this.overview()?.active_tasks || []);

  private timer: any = null;

  ngOnInit(): void {
    this.checkStatus();
    // Poll every 2 seconds for continuous synchronization of activities & metrics
    this.timer = setInterval(() => this.checkStatus(), 2000);
  }

  ngOnDestroy(): void {
    if (this.timer) clearInterval(this.timer);
  }

  checkStatus(): void {
    this.api.getOverview().subscribe({
      next: (data) => {
        this.overview.set(data);
        this.apiOnline.set(true);
      },
      error: () => {
        this.apiOnline.set(false);
      }
    });
  }
}

