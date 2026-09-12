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
type Snapshot = { artifacts: LibraryArtifact[]; locations: StorageLocations | null; loading:boolean; error:string | null; revision:number };
let snapshot: Snapshot = { artifacts:[], locations:null, loading:true, error:null, revision:0 };
const subscribers=new Set<()=>void>(); let inFlight=false;
function publish(next:Snapshot){ snapshot=next; subscribers.forEach((fn)=>fn()); }
function subscribe(fn:()=>void){ subscribers.add(fn); if(subscribers.size===1)void refreshLibrary(); return()=>{ subscribers.delete(fn); }; }
function getSnapshot(){ return snapshot; }
async function command<T=Record<string,unknown>>(operation:string,payload:Record<string,unknown>={}){ return invoke<T>("library_command",{operation,payload}); }
export async function refreshLibrary(){ if(inFlight)return; inFlight=true; try{ const result=await command<{ok:boolean;artifacts:LibraryArtifact[];locations:StorageLocations;error?:string}>("snapshot"); if(!result.ok)throw new Error(result.error||"Library snapshot failed."); publish({artifacts:result.artifacts,locations:result.locations,loading:false,error:null,revision:snapshot.revision+1}); }catch(error){ publish({...snapshot,loading:false,error:error instanceof Error?error.message:String(error),revision:snapshot.revision+1}); }finally{inFlight=false;} }
export async function markLibrarySeen(ids:string[]){ if(!ids.length)return; await command("mark_seen",{ids}); await refreshLibrary(); }
export async function libraryOpen(id:string){ await command("open",{id}); }
export async function libraryOpenFolder(id?:string){ await command("open_folder",id?{id}:{}); }
export async function libraryRename(id:string,name:string){ await command("rename",{id,name}); await refreshLibrary(); }
export async function libraryDelete(ids:string[],forever:boolean){ const result=await command<{ok:boolean;results:{id:string;ok:boolean;error?:string}[]}>(forever?"delete_forever":"delete",{ids}); await refreshLibrary(); return result.results||[]; }
export async function libraryPickFolder(){ const result=await command<{ok:boolean;path:string}>("pick_folder"); return result.path||""; }
export async function libraryCopy(ids:string[],destination:string){ const result=await command<{ok:boolean;results:{id:string;ok:boolean;error?:string}[]}>("copy",{ids,destination}); await refreshLibrary(); return result.results||[]; }
export async function libraryMove(ids:string[],destination:string){ const result=await command<{ok:boolean;results:{id:string;ok:boolean;error?:string}[]}>("move",{ids,destination}); await refreshLibrary(); return result.results||[]; }
export function useLibraryStore(){ const value=useSyncExternalStore(subscribe,getSnapshot,getSnapshot); useEffect(()=>{ if(value.loading)void refreshLibrary(); },[value.loading]); return {...value, unreadCount:value.artifacts.filter((a)=>a.readState==="NEW").length, duplicateCount:value.artifacts.filter((a)=>a.duplicateState==="DUPLICATE").length, linkErrorCount:value.artifacts.filter((a)=>a.availabilityStatus==="LINK_ERROR").length}; }
