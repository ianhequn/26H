# K230 Deployment and VOFA+

## Vision Input

```text
Sensor:  1920×1080
ROI:     centered 1080×1080
Model:   YOLOv5n, 224×224
Format:  RGB888 planar
```

The model predicts in 224 coordinates. The program maps each box center back to the original sensor coordinates.

## VOFA+ Packet

Every packet contains a fixed 16 numeric values:

```text
balls:count,x0,y0,score0,x1,y1,score1,...,x4,y4,score4\n
```

Missing slots use:

```text
-1,-1,0
```

Detections are sorted by x coordinate every frame:

```text
ball0 = leftmost ball in the current frame
ball1 = second ball in the current frame
...
```

These slots are not persistent target IDs.

## USB Debug Serial

The current test stage uses:

```python
print(packet, end="")
```

No FPIOA or hardware UART pin configuration is required. When VOFA output is enabled, other debug text is disabled so the FireWater parser receives only `balls:` lines.

## Runtime Safety

The program retains:

```python
finally:
    yolo.deinit()
    pipeline.destroy()
    gc.collect()
```

Garbage collection is also executed periodically during the loop.

## Competition Readiness Checks

- run continuously for at least 10-20 minutes
- verify camera exposure and focus after final mounting
- test close and overlapping balls
- confirm the complete loop timing, not inference-only timing
- test power-on startup without CanMV IDE
- test USB packet parsing under vehicle motion

