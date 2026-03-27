/**
 * Recursively converts string values in an object back to BigInt if they were stringified.
 * This is primarily for the AGW Session object fields that are BigInt.
 */
export function reviveBigInts(obj: any): any {
  if (obj === null || typeof obj !== "object") {
    return obj;
  }

  if (Array.isArray(obj)) {
    return obj.map(reviveBigInts);
  }

  const result: any = {};
  for (const [key, value] of Object.entries(obj)) {
    // Specifically handle session object keys that are bigints
    const bigintKeys = [
      "expiresAt", "limit", "period", "maxValuePerUse", "index"
    ];

    if (bigintKeys.includes(key) && typeof value === "string") {
      try {
        result[key] = BigInt(value);
      } catch {
        result[key] = value;
      }
    } else if (typeof value === "object") {
      result[key] = reviveBigInts(value);
    } else {
      result[key] = value;
    }
  }
  return result;
}
