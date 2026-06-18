export interface ContractTask {
  id: string;
  correspondenceNo: string;
  stepName: string;
  submittalDescription: string;
  reasonForIssue: string;
  submittalType: string;
  targetDate: string;
  creationDate: string;
  details: string;
  isClaimed: boolean;
  claimedBy?: string;
  status: 'All' | 'Submitted' | 'Closed' | 'Overdue';
  project: string;
  author: string;
  title: string;
  fromUser: string;
  toUser: string;
  revision?: string;
  vgRevName?: string;
  issueDate?: string;
  vgRevDescription?: string;
  returnStatus?: string;
  contractorDocNo?: string;
  purchaseOrder?: string;
  originator?: string;
  plannedIP?: string;
  collector?: string;
  revCreationDate?: string;
  revCreationUser?: string;
  lastUpdated?: string;
  docStatus?: string;
  versionCreationDate?: string;
  contract?: string;
  comment?: string;
  attachments?: string[];
  loadsheetVariant?: 'pass' | 'fake';
}

export interface Project {
  id: string;
  name: string;
}

// --- Bot validation report (pushed in from the headed automation bot) ---
export type BotStatus = 'reviewing' | 'pass' | 'fail';

export interface BotCheck {
  name: string;
  status: 'PASS' | 'FAIL' | 'WARN';
  detail: string;
}

export interface BotStage {
  title: string;
  ok: boolean;
  checks: BotCheck[];
}

export interface ValidationReport {
  loadsheet: string;
  overall: 'VALID' | 'INVALID';
  headline: string;
  reasons: string[];
  stages: BotStage[];
}
