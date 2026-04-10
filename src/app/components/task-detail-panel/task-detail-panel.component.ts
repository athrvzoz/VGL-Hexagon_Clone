import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AppStateService } from '../../services/app-state.service';

@Component({
  selector: 'app-task-detail-panel',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './task-detail-panel.component.html',
  styleUrl: './task-detail-panel.component.css'
})
export class TaskDetailPanelComponent {
  state = inject(AppStateService);
}
