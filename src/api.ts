export async function api<T>(path:string,options?:RequestInit):Promise<T>{
 const response=await fetch(`/api${path}`,{...options,headers:{'Content-Type':'application/json',...options?.headers}});
 if(!response.ok){let message=`Request failed (${response.status})`;try{const body=await response.json();message=typeof body.detail==='string'?body.detail:JSON.stringify(body.detail)}catch{/* use status */}throw new Error(message)}
 return response.json() as Promise<T>;
}
export const post=<T,>(path:string,body:unknown={})=>api<T>(path,{method:'POST',body:JSON.stringify(body)});
export function download(name:string,value:unknown){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const anchor=document.createElement('a');anchor.href=url;anchor.download=name;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
