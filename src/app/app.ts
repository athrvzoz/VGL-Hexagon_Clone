import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AppStateService } from './services/app-state.service';

// Components
import { HeaderComponent } from './components/header/header.component';
import { SidebarComponent } from './components/sidebar/sidebar.component';
import { DashboardComponent } from './components/dashboard/dashboard.component';
import { HistoryDetailComponent } from './components/history-detail/history-detail.component';
import { TaskDetailPanelComponent } from './components/task-detail-panel/task-detail-panel.component';
import { SideActionPanelComponent } from './components/side-action-panel/side-action-panel.component';
import { TransmittalModalComponent } from './components/transmittal-modal/transmittal-modal.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    CommonModule,
    HeaderComponent,
    SidebarComponent,
    DashboardComponent,
    HistoryDetailComponent,
    TaskDetailPanelComponent,
    SideActionPanelComponent,
    TransmittalModalComponent
  ],
  templateUrl: './app.html',
  styleUrl: './app.css'
})
export class App implements OnInit {
  state = inject(AppStateService);

  ngOnInit() {
    this.state.selectProject(this.state.selectedProject());
  }
}
