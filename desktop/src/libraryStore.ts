import { useEffect, useSyncExternalStore } from "react";
import { invoke } from "@tauri-apps/api/core";

export type ArtifactCategory = "image" | "video" | "pdf" | "document" | "audio" | "link" | "archive" | "unknown";
export type LibraryArtifact = {
  id: string; name: string; category: ArtifactCategory; extension: string; iconKey: string; mimeType: string;
  pathOrUrl: string; sizeBytes: number; createdAt: number; modifiedAt: number; origin: string; contentHash: string;
  duplicateGroupId: string; availabilityStatus: "AVAILABLE" | "LINK_ERROR"; duplicateState: "UNIQUE" | "DUPLICATE";
  readState: "NEW" | "SEEN"; sourceConversationId: string; sourceMessageId: string; sourceRequestId: string;
  thumbnailPath: string; lastVerifiedAt: number; linkErrorReason: string;
};
export type StorageLocations = { dataRoot:string; historyRoot:string; memoryRoot:string; libraryDbRoot:string; artifactRoot:string; cacheRoot:string; backupRoot:string };
// Read-only storage diagnostics from the backend: invalid stored locations are
// reported (the default folder is used meanwhile) and never rewritten silently.
export type StorageProblem = { key:string; label:string; code:string; message:string; stored:string };
export type StorageDiagnostics = { locatorStatus:"missing"|"ok"|"corrupt"|"newer"; locatorPath:string; writable:boolean; problems:StorageProblem[] };
type Snapshot = { artifacts: LibraryArtifact[]; locations: StorageLocations | null; storage: StorageDiagnostics | null; loading:boolean; error:string | null; revision:number };
let snapshot: Snapshot = { artifacts:[], locations:null, storage:null, loading:true, error:null, revision:0 };
const subscribers=new Set<()=>void>(); let inFlight=false;
function publish(next:Snapshot){ snapshot=next; subscribers.forEach((fn)=>fn()); }
function subscribe(fn:()=>void){ subscribers.add(fn); if(subscribers.size===1)void refreshLibrary(); return()=>{ subscribers.delete(fn); }; }
function getSnapshot(){ return snapshot; }
async function command<T=Record<string,unknown>>(operation:string,payload:Record<string,unknown>={}){ return invoke<T>("library_command",{operation,payload}); }
export async function refreshLibrary(){ if(inFlight)return; inFlight=true; try{ const result=await command<{ok:boolean;artifacts:LibraryArtifact[];locations:StorageLocations;storage?:StorageDiagnostics;error?:string}>("snapshot"); if(!result.ok)throw new Error(result.error||"Library snapshot failed."); publish({artifacts:result.artifacts,locations:result.locations,storage:result.storage??null,loading:false,error:null,revision:snapshot.revision+1}); }catch(error){ let storage=snapshot.storage; try{ const status=await command<{ok:boolean;locations:StorageLocations;storage:StorageDiagnostics}>("storage_status"); storage=status.storage; }catch{ /* diagnostics unavailable too */ } publish({...snapshot,storage,loading:false,error:error instanceof Error?error.message:String(error),revision:snapshot.revision+1}); }finally{inFlight=false;} }
// Explicit user choice of a new folder for one storage location. Existing files are not moved.
export async function librarySetStorageLocation(key:string,destination:string){ const result=await command<{ok:boolean;message:string;preservedLocator:string}>("set_storage_location",{key,destination}); await refreshLibrary(); return result; }
export async function markLibrarySeen(ids:string[]){ if(!ids.length)return; await command("mark_seen",{ids}); await refreshLibrary(); }
export async function libraryOpen(id:string){ await command("open",{id}); }
export async function libraryOpenFolder(id?:string){ await command("open_folder",id?{id}:{}); }
export async function libraryPickFolder(){ const result=await command<{ok:boolean;path:string}>("pick_folder"); return result.path||""; }
// File mutations are two-phase. The backend validates one exact operation and
// returns a single-use approval; commit runs only what that approval covers.
// Permanent deletion is hard-denied by the backend and has no client call.
export type LibraryMutation = "rename" | "copy" | "move" | "delete";
export type LibraryApproval = { token:string; operation:LibraryMutation; decision:string; confirmation:"none"|"click"|"secret"; expiresAt:number; destination:string; newName:string; targets:{id:string;name:string;pathOrUrl:string}[] };
export type LibraryItemResult = { id:string; ok:boolean; outcome:string; error?:string; newArtifactId?:string };
export type LibraryCommitResult = { results:LibraryItemResult[]; succeeded:number; failed:number; refused:number; complete:boolean };
export async function libraryPrepare(action:LibraryMutation,ids:string[],extra:{destination?:string;name?:string}={}){ const result=await command<{ok:boolean;approval:LibraryApproval}>("prepare",{action,ids,...extra}); return result.approval; }
// The approval secret is sent once with the commit and kept nowhere here.
export async function libraryCommit(approval:LibraryApproval,secret=""){ try{ return await command<LibraryCommitResult&{ok:boolean}>("commit",{action:approval.operation,token:approval.token,secret}); } finally { await refreshLibrary(); } }
export async function libraryCancel(approval:LibraryApproval){ try{ await command("cancel",{token:approval.token}); }catch{ /* an expired approval is already void */ } }
export function useLibraryStore(){ const value=useSyncExternalStore(subscribe,getSnapshot,getSnapshot); useEffect(()=>{ if(value.loading)void refreshLibrary(); },[value.loading]); return {...value, unreadCount:value.artifacts.filter((a)=>a.readState==="NEW").length, duplicateCount:value.artifacts.filter((a)=>a.duplicateState==="DUPLICATE").length, linkErrorCount:value.artifacts.filter((a)=>a.availabilityStatus==="LINK_ERROR").length}; }
