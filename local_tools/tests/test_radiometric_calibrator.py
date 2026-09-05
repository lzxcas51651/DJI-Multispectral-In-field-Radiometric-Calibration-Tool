import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from radiometric_calibrator.calibration import fit_models
from radiometric_calibrator.candidate import Candidate, find_candidates
from radiometric_calibrator.catalog import scan_folder
from radiometric_calibrator.geotiff import apply_models, describe_bands
from radiometric_calibrator.metadata import read_image_metadata
from radiometric_calibrator.project import COEFFICIENTS_FILENAME, save_coefficients
from radiometric_calibrator.registration import register_rgb_to_band, transform_polygon
from radiometric_calibrator.roi import RgbRoiAnnotation, RoiSample, roi_statistics


def sample(roi_id: str, band: str, dn: float, reflectance: float) -> RoiSample:
    return RoiSample(
        roi_id=roi_id,
        capture_key="capture",
        image_path="frame.tif",
        band=band,
        panel_id="panel",
        polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
        reflectance=reflectance,
        pixel_count=100,
        mean=dn,
        median=dn,
        trimmed_mean=dn,
        stddev=1.0,
        cv_percent=1.0 / dn * 100,
        minimum=dn - 2,
        maximum=dn + 2,
    )


class CatalogTests(unittest.TestCase):
    def test_sensor_can_be_read_from_manual_photo_metadata_without_route(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual-panel-photo.bin"
            path.write_bytes(
                b'prefix <tiff:Make>DJI</tiff:Make><tiff:Model>M3M</tiff:Model>'
                b'<Camera:BandName>Green</Camera:BandName> suffix'
            )
            metadata = read_image_metadata(path)
            self.assertEqual(metadata.sensor, "M3M")
            self.assertEqual(metadata.band_name, "Green")

    def test_m3m_detection_and_grouping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for capture in ("DJI_001", "DJI_002"):
                (root / f"{capture}_D.JPG").touch()
                for suffix in ("G", "R", "RE", "NIR"):
                    (root / f"{capture}_MS_{suffix}.TIF").touch()
            catalog = scan_folder(root)
            self.assertEqual(catalog.sensor, "M3M")
            self.assertEqual(len(catalog.captures), 2)
            self.assertEqual(len(catalog.complete_captures), 2)

    def test_p4m_detection_and_grouping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for capture in ("001", "002"):
                (root / f"DJI_{capture}0.JPG").touch()
                for code in "12345":
                    (root / f"DJI_{capture}{code}.TIF").touch()
            catalog = scan_folder(root)
            self.assertEqual(catalog.sensor, "P4M")
            self.assertEqual(len(catalog.complete_captures), 2)


class CalibrationTests(unittest.TestCase):
    def test_candidate_scan_includes_middle_of_large_batch(self):
        paths = [Path(f'image-{index:04d}.jpg') for index in range(401)]
        middle = paths[200]
        visited, progress = [], []
        def fake_score(path):
            visited.append(path)
            score = 0.9 if path == middle else 0.0
            return Candidate(path, score, (1, 1, 2, 2) if score else None)
        with patch('radiometric_calibrator.candidate._score', side_effect=fake_score):
            result = find_candidates(paths, progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(set(visited), set(paths))
        self.assertEqual(result[0].path, middle)
        self.assertEqual(progress[-1], (401, 401))

    def test_alpha_recognized_from_color_interpretation(self):
        import rasterio
        from rasterio.enums import ColorInterp
        from rasterio.transform import from_origin
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'alpha.tif'
            with rasterio.open(path, 'w', driver='GTiff', width=2, height=2, count=2,
                               dtype='uint16', photometric='MINISBLACK', ALPHA='YES',
                               transform=from_origin(10, 20, 1, 1)) as dst:
                dst.write(np.ones((2, 2, 2), dtype='uint16'))
                dst.colorinterp = (ColorInterp.gray, ColorInterp.alpha)
            self.assertEqual(describe_bands(path)[-1], 'Alpha')

    def test_output_band_order_follows_source_not_models(self):
        import rasterio
        from rasterio.transform import from_origin
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'dn.tif', Path(directory) / 'out.tif'
            with rasterio.open(source, 'w', driver='GTiff', width=2, height=2,
                               count=3, dtype='uint16', transform=from_origin(10, 20, 1, 1)) as dst:
                for index in range(1, 4):
                    dst.write(np.full((2, 2), index * 100, dtype='uint16'), index)
                dst.update_tags(TIFFTAG_SOFTWARE='source stitcher', flight='test-flight')
                dst.update_tags(ns='CUSTOM', camera='M3M')
                dst.update_tags(1, wavelength='560', STATISTICS_MEAN='100')
                dst.scales = (2.0, 2.0, 2.0)
            model = {'slope': 0.001, 'intercept': 0, 'method': 'test'}
            # Deliberately reversed coefficient and mapping insertion order.
            models = {'NIR': model, 'Red': model, 'Green': model}
            apply_models(source, output, models, {'NIR': 3, 'Red': 2, 'Green': 1})
            with rasterio.open(output) as dst:
                self.assertEqual(dst.descriptions, ('Green', 'Red', 'NIR'))
                np.testing.assert_allclose(dst.read()[:, 0, 0], [0.1, 0.2, 0.3], rtol=1e-6)
                self.assertEqual(dst.tags(3)['source_band_index'], '3')
                self.assertEqual(dst.tags()['flight'], 'test-flight')
                self.assertEqual(dst.tags(ns='CUSTOM')['camera'], 'M3M')
                self.assertEqual(dst.tags(1)['wavelength'], '560')
                self.assertNotIn('STATISTICS_MEAN', dst.tags(1))
                self.assertEqual(dst.scales, (1.0, 1.0, 1.0))
                original = json.loads(dst.tags(ns='SOURCE_METADATA')['original_json'])
                self.assertEqual(original['bands']['1']['default']['STATISTICS_MEAN'], '100')
                self.assertEqual(original['scales'], [2.0, 2.0, 2.0])
            apply_models(source, output, {'NIR': model, 'Green': model}, {'NIR': 3, 'Green': 1})
            with rasterio.open(output) as dst:
                self.assertEqual(dst.descriptions, ('Green', 'NIR'))
                self.assertEqual(dst.tags(2)['source_band_index'], '3')

    def test_single_level_fits_through_origin(self):
        models = fit_models([sample("ROI-001", "Red", 1000, 0.5), sample("ROI-002", "Red", 1000, 0.5)])
        self.assertAlmostEqual(models["Red"].slope, 0.0005)
        self.assertEqual(models["Red"].intercept, 0.0)
        self.assertEqual(models["Red"].method, "least_squares_through_origin")

    def test_multiple_levels_fit_line(self):
        models = fit_models([
            sample("ROI-001", "NIR", 100, 0.2),
            sample("ROI-002", "NIR", 300, 0.6),
            sample("ROI-003", "NIR", 400, 0.8),
        ])
        self.assertAlmostEqual(models["NIR"].slope, 0.002, places=8)
        self.assertAlmostEqual(models["NIR"].intercept, 0.0, places=8)

    def test_fixed_english_filename(self):
        models = fit_models([sample("ROI-001", "Green", 500, 0.5)])
        with tempfile.TemporaryDirectory() as directory:
            output = save_coefficients(directory, directory, "M3M", [sample("ROI-001", "Green", 500, 0.5)], models)
            self.assertEqual(output.name, "radiometric_calibration_coefficients.json")
            self.assertEqual(output.name, COEFFICIENTS_FILENAME)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("Green", payload["models"])

    def test_rgb_annotation_is_saved_for_registration_traceability(self):
        models = fit_models([sample("ROI-001-Green", "Green", 500, 0.5)])
        annotation = RgbRoiAnnotation(
            "ROI-001", "capture", "rgb.jpg", "panel", [[1, 1], [9, 1], [9, 9], [1, 9]], {"Green": 0.5}
        )
        with tempfile.TemporaryDirectory() as directory:
            output = save_coefficients(
                directory,
                directory,
                "M3M",
                [sample("ROI-001-Green", "Green", 500, 0.5)],
                models,
                rgb_annotations=[annotation],
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["rgb_annotations"][0]["roi_id"], "ROI-001")

    def test_apply_model_writes_float32_reflectance(self):
        try:
            import rasterio
            from rasterio.transform import from_origin
        except ImportError:
            self.skipTest("rasterio is not installed")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "dn.tif"
            output = Path(directory) / "reflectance.tif"
            profile = {
                "driver": "GTiff",
                "width": 3,
                "height": 2,
                "count": 1,
                "dtype": "uint16",
                "crs": "EPSG:32650",
                "transform": from_origin(500000, 3400000, 0.05, 0.05),
            }
            with rasterio.open(source, "w", **profile) as dataset:
                dataset.write(np.asarray([[100, 200, 300], [400, 500, 600]], dtype=np.uint16), 1)
                dataset.set_band_description(1, "Green")
            models = {
                "Green": {
                    "slope": 0.001,
                    "intercept": 0.01,
                    "method": "linear_least_squares",
                }
            }
            updates = []
            apply_models(source, output, models, {"Green": 1}, progress=lambda p, m: updates.append(p))
            self.assertEqual(updates[-1], 100)
            self.assertEqual(updates, sorted(updates))
            previous = output.read_bytes()
            with self.assertRaises(ValueError):
                apply_models(source, output, {**models, 'Red': models['Green']}, {'Green': 1, 'Red': 1})
            with self.assertRaises(ValueError):
                apply_models(source, source, models, {'Green': 1})
            with self.assertRaises(ValueError):
                apply_models(source, output, models, {'Green': 2})
            with self.assertRaises(KeyError):
                apply_models(source, output, {'Green': {}}, {'Green': 1})
            self.assertEqual(output.read_bytes(), previous)
            self.assertFalse(list(output.parent.glob('.reflectance-*')))
            with rasterio.open(output) as dataset:
                self.assertEqual(dataset.dtypes[0], "float32")
                self.assertEqual(dataset.descriptions[0], "Green")
                np.testing.assert_allclose(
                    dataset.read(1),
                    np.asarray([[0.11, 0.21, 0.31], [0.41, 0.51, 0.61]], dtype=np.float32),
                    rtol=1e-6,
                )

    def test_roi_statistics_and_fast_candidate(self):
        import cv2

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "panel.jpg"
            image = np.full((500, 700, 3), 35, dtype=np.uint8)
            cv2.rectangle(image, (100, 120), (290, 300), (210, 210, 210), -1)
            cv2.rectangle(image, (390, 160), (590, 350), (90, 90, 90), -1)
            ok, encoded = cv2.imencode(".jpg", image)
            self.assertTrue(ok)
            encoded.tofile(str(path))
            candidates = find_candidates([path])
            self.assertEqual(candidates[0].path, path)
            self.assertGreaterEqual(candidates[0].panel_count, 2)
            self.assertGreaterEqual(len(candidates[0].rectangles), 2)

            colored_path = Path(directory) / "colored.jpg"
            colored = np.full((500, 700, 3), 35, dtype=np.uint8)
            cv2.rectangle(colored, (180, 140), (520, 370), (0, 0, 230), -1)
            ok, encoded = cv2.imencode('.jpg', colored)
            self.assertTrue(ok)
            encoded.tofile(str(colored_path))
            self.assertEqual(find_candidates([colored_path]), [])

            gray_path = Path(directory) / "panel.tif"
            gray = np.full((100, 100), 1000, dtype=np.uint16)
            ok, encoded = cv2.imencode(".tif", gray)
            self.assertTrue(ok)
            encoded.tofile(str(gray_path))
            stats = roi_statistics(gray_path, [[10, 10], [90, 10], [90, 90], [10, 90]])
            self.assertAlmostEqual(stats["trimmed_mean"], 1000.0)

    def test_lightweight_rgb_to_band_registration(self):
        import cv2

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rgb = np.zeros((500, 700, 3), dtype=np.uint8)
            rng = np.random.default_rng(42)
            for _ in range(80):
                x, y = rng.integers([20, 20], [680, 480])
                cv2.circle(rgb, (int(x), int(y)), int(rng.integers(3, 12)), (220, 220, 220), -1)
            band = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY), (350, 250), interpolation=cv2.INTER_AREA)
            rgb_path, band_path = root / "rgb.jpg", root / "green.tif"
            cv2.imencode(".jpg", rgb)[1].tofile(str(rgb_path))
            cv2.imencode(".tif", band.astype(np.uint16) * 100)[1].tofile(str(band_path))
            result = register_rgb_to_band(rgb_path, band_path)
            mapped = transform_polygon([[100, 100], [200, 100], [200, 200], [100, 200]], result.matrix)
            np.testing.assert_allclose(mapped[0], [50, 50], atol=5)
            self.assertIn(result.method, {"ORB-Homography", "ECC-Affine"})


if __name__ == "__main__":
    unittest.main()
