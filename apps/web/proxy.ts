import { timingSafeEqual } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";

export function proxy(request: NextRequest) {
  if (process.env.NEXT_PUBLIC_F1RI_PRIVATE !== "true") return NextResponse.next();
  const expected = Buffer.from(process.env.F1RI_EDGE_TOKEN ?? "");
  const received = Buffer.from(request.headers.get("x-f1ri-web-gateway") ?? "");
  if (expected.length < 32 || expected.length !== received.length || !timingSafeEqual(expected, received)) {
    return new NextResponse("Use the private HTTPS gateway.", { status: 403, headers: { "Cache-Control": "no-store" } });
  }
  return NextResponse.next();
}
