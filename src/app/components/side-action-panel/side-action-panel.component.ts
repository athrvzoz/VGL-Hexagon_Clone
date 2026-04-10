import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AppStateService } from '../../services/app-state.service';

@Component({
  selector: 'app-side-action-panel',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './side-action-panel.component.html',
  styleUrl: './side-action-panel.component.css'
})
export class SideActionPanelComponent {
  state = inject(AppStateService);

  closeSidePanel() {
    this.state.isSidePanelOpen.set(false);
  }
}
