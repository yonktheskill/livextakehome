import { api, type Attachment } from "./api";

export const MAX_IMAGES = 4;
export const MAX_INPUT_BYTES = 10 * 1024 * 1024;
export const MAX_IMAGE_BYTES = 2 * 1024 * 1024;
export const SPLIT_GUIDANCE =
  "Crop or split this screenshot into readable sections, then attach them separately.";

export async function prepareImage(file: File): Promise<Blob> {
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type))
    throw new Error("Attach a static JPEG, PNG, or WebP image. GIF, SVG, PDF, and HEIC are unsupported.");
  if (file.size > MAX_INPUT_BYTES)
    throw new Error("Each original image must be 10 MiB or smaller. " + SPLIT_GUIDANCE);
  if (!file.size) throw new Error("This image is empty.");
  // Do not flatten an animated file into a misleading first frame.
  const bytes = new Uint8Array(await file.arrayBuffer());
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const tag = (offset: number) => String.fromCharCode(...bytes.subarray(offset, offset + 4));
  let animated = false;
  if (file.type === "image/png") {
    for (let offset = 8; offset + 12 <= bytes.length; ) {
      const size = view.getUint32(offset);
      if (tag(offset + 4) === "acTL") {
        animated = true;
        break;
      }
      if (size > bytes.length - offset - 12) break;
      offset += size + 12;
    }
  } else if (file.type === "image/webp") {
    for (let offset = 12; offset + 8 <= bytes.length; ) {
      const size = view.getUint32(offset + 4, true);
      if (["ANIM", "ANMF"].includes(tag(offset))) {
        animated = true;
        break;
      }
      if (size > bytes.length - offset - 8) break;
      offset += size + 8 + (size % 2);
    }
  }
  if (animated) throw new Error("Animated images are unsupported. Attach a static screenshot.");
  const image = await createImageBitmap(file, { imageOrientation: "from-image" });
  try {
    const scale = Math.min(
      1,
      4096 / Math.max(image.width, image.height),
      Math.sqrt(8_000_000 / (image.width * image.height)),
    );
    // Large reductions make small screenshot text unreadable. Ask for intentional crops.
    if (scale < 0.75)
      throw new Error("This image is too large to resize without losing readability. " + SPLIT_GUIDANCE);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.floor(image.width * scale));
    canvas.height = Math.max(1, Math.floor(image.height * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Image processing is unavailable in this browser.");
    context.fillStyle = "white";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const encode = (mime: string, quality?: number) =>
      new Promise<Blob>((resolve, reject) =>
        canvas.toBlob(
          (blob) => (blob ? resolve(blob) : reject(new Error("Unable to process this image."))),
          mime,
          quality,
        ),
      );
    const png = await encode("image/png");
    if (png.size <= MAX_IMAGE_BYTES) return png;
    for (const quality of [0.9, 0.82, 0.74]) {
      const jpeg = await encode("image/jpeg", quality);
      if (jpeg.size <= MAX_IMAGE_BYTES) return jpeg;
    }
    throw new Error(
      "This image cannot fit the 2 MiB limit while preserving readable detail. " + SPLIT_GUIDANCE,
    );
  } finally {
    image.close();
  }
}

export async function uploadImage(blob: Blob): Promise<Attachment> {
  return api<Attachment>("/chat/attachments", {
    method: "POST",
    headers: { "Content-Type": blob.type },
    body: blob,
  });
}
