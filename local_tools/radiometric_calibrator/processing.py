"""Background operations: never access Qt widgets from here."""
from .calibration import fit_models
from .project import save_coefficients
from .registration import register_rgb_to_band, transform_polygon
from .roi import RoiSample, roi_statistics


def calculate_coefficients(catalog, annotations, progress):
    enabled = [item for item in annotations if item.enabled]
    if not enabled:
        raise ValueError("请至少启用一个 ROI。")
    total = len(enabled) * len(catalog.expected_bands) + 2
    done = 0
    generated, cache, review = [], {}, []
    for annotation in enabled:
        capture = next((item for item in catalog.captures if item.key == annotation.capture_key), None)
        if capture is None:
            raise ValueError(f"找不到 {annotation.roi_id} 对应的曝光组。")
        missing = [band for band in ('RGB', *catalog.expected_bands) if band not in capture.files]
        if missing:
            raise ValueError(f"{annotation.roi_id} 对应曝光组缺少：{', '.join(missing)}")
        for band in catalog.expected_bands:
            progress(int(done * 100 / total), f"正在配准和统计 {annotation.roi_id} → {band}")
            key = (capture.key, band)
            if key not in cache:
                cache[key] = register_rgb_to_band(capture.files['RGB'], capture.files[band])
            registration = cache[key]
            if (registration.method.endswith('Fallback')
                    or (registration.method == 'ORB-Homography' and registration.score < 0.35)
                    or (registration.method == 'ECC-Affine' and registration.score < 0.5)):
                review.append(f"{annotation.roi_id}/{band} ({registration.method}, {registration.score:.3f})")
            polygon = transform_polygon(annotation.polygon, registration.matrix)
            try:
                stats = roi_statistics(capture.files[band], polygon)
            except Exception as exc:
                raise ValueError(f"{annotation.roi_id} → {band} 无法统计：{exc}；"
                                 f"配准 {registration.method}, {registration.score:.3f}") from exc
            generated.append(RoiSample(
                roi_id=f'{annotation.roi_id}-{band}', capture_key=capture.key,
                image_path=str(capture.files[band]), band=band, panel_id=annotation.panel_id,
                polygon=polygon, reflectance=annotation.reflectance_by_band[band],
                source_rgb_roi_id=annotation.roi_id, source_rgb_path=annotation.image_path,
                source_rgb_polygon=annotation.polygon, registration_method=registration.method,
                registration_score=registration.score, **stats))
            done += 1
    progress(int(done * 100 / total), '正在拟合定标系数')
    models = fit_models(generated)
    if not models:
        raise ValueError('配准后没有可用于拟合的有效波段 ROI。')
    progress(int((done + 1) * 100 / total), '正在保存系数文件')
    output = save_coefficients(catalog.root, catalog.root, catalog.sensor, generated, models,
                               rgb_annotations=annotations)
    progress(100, '系数已保存')
    return generated, models, output, review
